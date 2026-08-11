#!/usr/bin/env bash
set -Eeuo pipefail

umask 0077

readonly DATABASE="moon_poro_prod"
readonly BACKUP_DIR="/var/backups/moon-poro-prod"
readonly MIN_FREE_KIB=$((1024 * 1024))

if [[ "$(id -un)" != "postgres" ]]; then
    echo "This backup must run as the postgres system user." >&2
    exit 1
fi

if [[ ! -d "$BACKUP_DIR" || ! -w "$BACKUP_DIR" ]]; then
    echo "Backup directory is missing or not writable: $BACKUP_DIR" >&2
    exit 1
fi

exec 9>"$BACKUP_DIR/.backup.lock"
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

echo "Verified PostgreSQL backup created: $dump_path"
echo "Checksum: $checksum_path"
