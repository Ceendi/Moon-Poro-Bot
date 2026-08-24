#!/usr/bin/env bash
set -Eeuo pipefail

umask 0077

readonly DATABASE="moon_poro_prod"
readonly BACKUP_ROOT="/var/backups/moon-poro-prod"
readonly BACKUP_KIND="${1:-}"
readonly RETENTION_DAYS=90
readonly MIN_FREE_KIB=$((1024 * 1024))
readonly BACKUP_NAME_PATTERN='^moon_poro_prod-[0-9]{8}T[0-9]{6}Z\.dump$'

case "$BACKUP_KIND" in
    daily | pre-deploy) ;;
    *)
        echo "Usage: $0 {daily|pre-deploy}" >&2
        exit 1
        ;;
esac

readonly BACKUP_DIR="$BACKUP_ROOT/$BACKUP_KIND"

if [[ "$(id -un)" != "postgres" ]]; then
    echo "This backup must run as the postgres system user." >&2
    exit 1
fi

if [[ ! -d "$BACKUP_DIR" || ! -w "$BACKUP_DIR" ]]; then
    echo "Backup directory is missing or not writable: $BACKUP_DIR" >&2
    exit 1
fi

exec 9>"$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then
    echo "Another Moon Poro database backup is already running." >&2
    exit 1
fi

current_database="$(
    psql --dbname="$DATABASE" --no-align --tuples-only --quiet \
        --command="SELECT current_database()"
)"
if [[ "$current_database" != "$DATABASE" ]]; then
    echo "Refusing backup: connected to '$current_database', expected '$DATABASE'." >&2
    exit 1
fi

available_kib="$(df --output=avail -k "$BACKUP_DIR" | tail -n 1 | tr -d ' ')"
if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || ((available_kib < MIN_FREE_KIB)); then
    echo "Refusing backup: less than 1 GiB is available on the backup filesystem." >&2
    exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_name="${DATABASE}-${timestamp}.dump"
dump_path="$BACKUP_DIR/$dump_name"
partial_path="$BACKUP_DIR/.${dump_name}.part"
checksum_path="${dump_path}.sha256"

if [[ -e "$dump_path" || -e "$checksum_path" ]]; then
    echo "Refusing to overwrite an existing backup for timestamp $timestamp." >&2
    exit 1
fi

cleanup_partial() {
    rm -f -- "$partial_path"
}

prune_expired_backups() {
    local backup_dir="$1"
    local old_dump old_name old_checksum

    while IFS= read -r -d '' old_dump; do
        old_name="${old_dump##*/}"
        if [[ ! "$old_name" =~ $BACKUP_NAME_PATTERN ]]; then
            echo "Skipping unexpected backup name: $old_name" >&2
            continue
        fi
        old_checksum="${old_dump}.sha256"
        rm -- "$old_dump"
        if [[ -f "$old_checksum" ]]; then
            rm -- "$old_checksum"
        fi
        echo "Removed expired backup: $old_name"
    done < <(
        find "$backup_dir" -maxdepth 1 -type f -name "${DATABASE}-*.dump" \
            -mtime "+$RETENTION_DAYS" -print0
    )
}

trap cleanup_partial EXIT

pg_dump --dbname="$DATABASE" --format=custom --file="$partial_path"
pg_restore --list "$partial_path" >/dev/null
mv -- "$partial_path" "$dump_path"
(
    cd -- "$BACKUP_DIR"
    sha256sum -- "$dump_name" >"${dump_name}.sha256"
)
chmod 0600 -- "$dump_path" "$checksum_path"
trap - EXIT

prune_expired_backups "$BACKUP_ROOT/daily"
prune_expired_backups "$BACKUP_ROOT/pre-deploy"

echo "Verified $BACKUP_KIND PostgreSQL backup created: $dump_path"
echo "Checksum: $checksum_path"
