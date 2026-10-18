#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TOOL_ENV="/opt/moon-poro-deploy-tools"
readonly RUNTIME_DIR="/opt/moon-poro-prod-runtime"
readonly BACKUP_DIR="/var/backups/moon-poro-prod"
readonly CLUSTER_CONFIG="/etc/postgresql/18/moon-poro-prod/postgresql.conf"

if ((EUID != 0)); then
    echo "Run this installer as root." >&2
    exit 1
fi
if ! getent passwd postgres >/dev/null; then
    echo "The postgres system user does not exist." >&2
    exit 1
fi
# The backup units require the cluster unit, which needs this cluster.
if [[ ! -f "$CLUSTER_CONFIG" ]]; then
    echo "PostgreSQL cluster 18/moon-poro-prod does not exist: $CLUSTER_CONFIG" >&2
    exit 1
fi

install -d -o root -g root -m 0755 "$TOOL_ENV" "$RUNTIME_DIR"
install -d -o postgres -g postgres -m 0700 \
    "$BACKUP_DIR" "$BACKUP_DIR/daily" "$BACKUP_DIR/pre-deploy"

if [[ ! -x "$TOOL_ENV/bin/python" ]]; then
    python3 -m venv "$TOOL_ENV"
fi
"$TOOL_ENV/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
    "uv==0.12.3"

install -o root -g root -m 0755 \
    "$SCRIPT_DIR/backup-prod-db.sh" /usr/local/sbin/backup-moon-poro-prod-db
install -o root -g root -m 0755 \
    "$SCRIPT_DIR/upload-prod-backup.sh" /usr/local/sbin/upload-moon-poro-prod-backup
install -o root -g root -m 0755 \
    "$SCRIPT_DIR/deploy-prod.sh" /usr/local/sbin/deploy-moon-poro-prod
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/postgresql-moon-poro-prod.service" \
    /etc/systemd/system/postgresql-moon-poro-prod.service
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-backup.service" \
    /etc/systemd/system/moon-poro-prod-backup.service
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-pre-deploy-backup.service" \
    /etc/systemd/system/moon-poro-prod-pre-deploy-backup.service
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-backup.timer" \
    /etc/systemd/system/moon-poro-prod-backup.timer
# Started by the backup units; skipped until /etc/moon-poro/prod-backup-offsite.env exists.
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-offsite-backup.service" \
    /etc/systemd/system/moon-poro-prod-offsite-backup.service
# Copied only when it differs and never restarted here: a running bot keeps
# its current settings until the next deployment restarts it.
bot_unit_changed=false
if ! cmp -s "$SCRIPT_DIR/moon-poro-prod.service" /etc/systemd/system/moon-poro-prod.service; then
    install -o root -g root -m 0644 \
        "$SCRIPT_DIR/moon-poro-prod.service" \
        /etc/systemd/system/moon-poro-prod.service
    bot_unit_changed=true
fi
# Installed but not enabled: RSO starts only after /etc/moon-poro/prod-rso.env exists.
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-rso.service" \
    /etc/systemd/system/moon-poro-prod-rso.service

systemctl daemon-reload
systemctl enable --now moon-poro-prod-backup.timer
# Enable only, so the bot starts at boot; the deployment starts it.
systemctl enable moon-poro-prod.service

if [[ "$bot_unit_changed" == true ]] && systemctl is-active --quiet moon-poro-prod.service; then
    echo "moon-poro-prod.service changed; it applies after the next bot restart:"
    echo "  sudo systemctl restart moon-poro-prod.service"
fi

echo "Production deployment command: sudo /usr/local/sbin/deploy-moon-poro-prod"
echo "Manual verified backup: sudo systemctl start moon-poro-prod-backup.service"
echo "Daily backups remain for 90 days in $BACKUP_DIR/daily."
echo "Pre-deploy backups also remain for 90 days in $BACKUP_DIR/pre-deploy."
echo "Cloud Storage copies: set BACKUP_GCS_BUCKET in /etc/moon-poro/prod-backup-offsite.env."
echo "Enable Riot Sign On once: sudo systemctl enable --now moon-poro-prod-rso.service"
