#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPOSITORY="/opt/moon-poro-prod"
readonly SERVICE="moon-poro-prod.service"
# The Riot Sign On callback shares this checkout and database. It is optional
# until RSO is enabled; when enabled it is updated together with the bot.
readonly RSO_SERVICE="moon-poro-prod-rso.service"
readonly BACKUP_SERVICE="moon-poro-prod-pre-deploy-backup.service"
readonly UV="/opt/moon-poro-deploy-tools/bin/uv"
readonly UV_RUNTIME="/opt/moon-poro-prod-runtime"
readonly DEPLOYED_COMMIT_FILE="$UV_RUNTIME/deployed-commit"
readonly EXPECTED_REMOTE="https://github.com/Ceendi/Moon-Poro-Bot.git"
readonly SERVICE_USER="moon-poro-prod"

assume_yes=false
case "${1:-}" in
    "") ;;
    --yes) assume_yes=true ;;
    *)
        echo "Usage: deploy-moon-poro-prod [--yes]" >&2
        exit 1
        ;;
esac

if ((EUID != 0)); then
    echo "Run this deployment as root." >&2
    exit 1
fi

exec 9>"/run/lock/moon-poro-prod-deploy.lock"
if ! flock -n 9; then
    echo "Another Moon Poro deployment is already running." >&2
    exit 1
fi

if [[ ! -d "$REPOSITORY/.git" ]]; then
    echo "Production directory is not a Git repository: $REPOSITORY" >&2
    exit 1
fi
if [[ ! -x "$UV" ]]; then
    echo "Pinned uv executable is missing: $UV" >&2
    exit 1
fi
if [[ ! -f "/etc/moon-poro/prod-bot.env" ]]; then
    echo "Production environment file is missing." >&2
    exit 1
fi

remote_url="$(git -C "$REPOSITORY" remote get-url origin)"
if [[ "$remote_url" != "$EXPECTED_REMOTE" ]]; then
    echo "Refusing deployment from unexpected origin: $remote_url" >&2
    exit 1
fi

if [[ -n "$(git -C "$REPOSITORY" status --porcelain)" ]]; then
    echo "Refusing deployment because production contains local or untracked changes." >&2
    git -C "$REPOSITORY" status --short
    exit 1
fi

rso_enabled=false
# A callback started by hand without "enable" must not keep old code running either.
if systemctl is-enabled --quiet "$RSO_SERVICE" 2>/dev/null \
    || systemctl is-active --quiet "$RSO_SERVICE"; then
    rso_enabled=true
fi

services_active() {
    systemctl is-active --quiet "$SERVICE" || return 1
    if [[ "$rso_enabled" == true ]]; then
        systemctl is-active --quiet "$RSO_SERVICE" || return 1
    fi
}

git -C "$REPOSITORY" fetch --prune origin master
current_commit="$(git -C "$REPOSITORY" rev-parse HEAD)"
target_commit="$(git -C "$REPOSITORY" rev-parse refs/remotes/origin/master)"
deployed_commit=""
if [[ -f "$DEPLOYED_COMMIT_FILE" ]]; then
    deployed_commit="$(<"$DEPLOYED_COMMIT_FILE")"
fi

if [[ "$current_commit" == "$target_commit" ]]; then
    if [[ "$deployed_commit" == "$target_commit" ]] && services_active; then
        echo "Moon Poro is already at origin/master ($current_commit) and the services are active."
        exit 0
    fi
    echo "Moon Poro is already at origin/master, but deployment is incomplete; resuming."
elif ! git -C "$REPOSITORY" merge-base --is-ancestor "$current_commit" "$target_commit"; then
    echo "Refusing non-fast-forward deployment: $current_commit -> $target_commit" >&2
    exit 1
else
    # Everything on origin/master runs as root below, so show what is about to
    # run and let the operator stop a commit they do not recognise.
    echo "Commits to deploy ($current_commit -> $target_commit):"
    git -C "$REPOSITORY" log --oneline --no-decorate "$current_commit..$target_commit"
    if [[ -n "$(git -C "$REPOSITORY" diff --name-only "$current_commit" "$target_commit" -- deploy/)" ]]; then
        echo "Files in deploy/ changed; run install-prod-automation.sh after this deployment."
    fi
    if [[ "$assume_yes" != true ]]; then
        if [[ ! -t 0 ]]; then
            echo "No terminal to confirm; rerun with --yes after checking the commits." >&2
            exit 1
        fi
        read -r -p "Deploy these commits? [y/N] " answer
        if [[ "$answer" != [yY] ]]; then
            echo "Deployment cancelled; production was not changed."
            exit 1
        fi
    fi
fi

if ! systemctl start "$BACKUP_SERVICE"; then
    echo "Verified database backup failed; production code was not changed." >&2
    systemctl --no-pager --full status "$BACKUP_SERVICE" || true
    exit 1
fi
if [[ "$(systemctl show "$BACKUP_SERVICE" --property=Result --value)" != "success" ]]; then
    echo "Verified database backup failed; production code was not changed." >&2
    systemctl --no-pager --full status "$BACKUP_SERVICE" || true
    exit 1
fi

git -C "$REPOSITORY" merge --ff-only "$target_commit"

export UV_CACHE_DIR="$UV_RUNTIME/cache"
export UV_PYTHON_INSTALL_DIR="$UV_RUNTIME/python"
(
    cd -- "$REPOSITORY"
    "$UV" sync --frozen --no-dev --python 3.14
)
"$REPOSITORY/.venv/bin/python" -m compileall -q "$REPOSITORY/moon_poro"

# Stop the old code before adding constraints that require fields it does not
# write. Migration 20260824_0007 reads LEGACY_AUDIT_CHANNEL_ID from the
# validated production environment.
# The callback writes the same tables, so it must not run old code against a
# migrated schema either.
if [[ "$rso_enabled" == true ]]; then
    systemctl stop "$RSO_SERVICE"
fi
systemctl stop "$SERVICE"
# Migrations run as the bot's system account: it can read the env file, and
# the database login in it owns the schema, so new code never needs root here.
if ! (
    cd -- "$REPOSITORY"
    runuser -u "$SERVICE_USER" -- env PYTHONDONTWRITEBYTECODE=1 \
        "$REPOSITORY/.venv/bin/python" -m moon_poro.migrate \
        --env-file /etc/moon-poro/prod-bot.env
); then
    echo "Database migration failed; Moon Poro services remain stopped to protect data." >&2
    exit 1
fi

systemctl start "$SERVICE"
sleep 8
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "Production service failed after deployment to $target_commit." >&2
    echo "Previous code commit was $current_commit; the database backup was preserved." >&2
    systemctl --no-pager --full status "$SERVICE" || true
    exit 1
fi

if [[ "$rso_enabled" == true ]]; then
    systemctl start "$RSO_SERVICE"
    sleep 3
    if ! systemctl is-active --quiet "$RSO_SERVICE"; then
        echo "Riot Sign On callback failed after deployment to $target_commit." >&2
        echo "Previous code commit was $current_commit; the database backup was preserved." >&2
        systemctl --no-pager --full status "$RSO_SERVICE" || true
        exit 1
    fi
fi

deployed_commit_tmp="$DEPLOYED_COMMIT_FILE.tmp.$$"
printf '%s\n' "$target_commit" >"$deployed_commit_tmp"
mv -f -- "$deployed_commit_tmp" "$DEPLOYED_COMMIT_FILE"

echo "Moon Poro deployed successfully: $current_commit -> $target_commit"
# Status output is informational; a unit that is restarting must not fail a done deploy.
systemctl --no-pager --full status "$SERVICE" || true
if [[ "$rso_enabled" == true ]]; then
    systemctl --no-pager --full status "$RSO_SERVICE" || true
fi
