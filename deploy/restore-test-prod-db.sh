#!/usr/bin/env bash
set -Eeuo pipefail

umask 0077

readonly PROD_DATABASE="moon_poro_prod"
readonly RESTORE_DATABASE="moon_poro_restore_test"
readonly BACKUP_DIR="/var/backups/moon-poro-prod/daily"
readonly DATA_DIR="/var/lib/postgresql/18/moon-poro-prod"
readonly MAX_DUMP_AGE_DAYS=2
readonly MIN_FREE_KIB=$((1024 * 1024))
readonly BACKUP_NAME_PATTERN='^moon_poro_prod-[0-9]{8}T[0-9]{6}Z\.dump$'
readonly TABLE_NAME_PATTERN='^public\.[a-z_][a-z0-9_]*$'
readonly KEY_TABLES=(verification_links warnings moderation_stats guild_features)

# The test database lives on the production cluster, so keep the server side of the
# restore small: the VM is an e2-micro with 1 GB RAM.
export PGCLUSTER="18/moon-poro-prod"
export PGHOST="/var/run/postgresql"
export PGPORT=5433
export PGOPTIONS="-c maintenance_work_mem=16MB -c work_mem=4MB -c client_min_messages=warning"

if [[ "$RESTORE_DATABASE" == "$PROD_DATABASE" ]]; then
    echo "Refusing restore test: the test database must not be $PROD_DATABASE." >&2
    exit 1
fi

if [[ "$(id -un)" != "postgres" ]]; then
    echo "The restore test must run as the postgres system user." >&2
    exit 1
fi

# Every command below names the test database (or the postgres maintenance database);
# nothing here connects to the production database.
psql_test() {
    psql --dbname="$RESTORE_DATABASE" --no-psqlrc --no-align --tuples-only --quiet \
        --set=ON_ERROR_STOP=1 "$@"
}

drop_restore_database() {
    dropdb --if-exists --force "$RESTORE_DATABASE"
}

dump_counts=""
restore_started=false
cleanup() {
    if [[ "$restore_started" == true ]]; then
        drop_restore_database || echo "Could not remove $RESTORE_DATABASE." >&2
    fi
    if [[ -n "$dump_counts" ]]; then
        rm -f -- "$dump_counts"
    fi
}
trap cleanup EXIT

latest_dump=""
while IFS= read -r -d '' dump; do
    name="${dump##*/}"
    if [[ ! "$name" =~ $BACKUP_NAME_PATTERN ]]; then
        continue
    fi
    if [[ -z "$latest_dump" || "$name" > "${latest_dump##*/}" ]]; then
        latest_dump="$dump"
    fi
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "${PROD_DATABASE}-*.dump" -print0)

if [[ -z "$latest_dump" ]]; then
    echo "No daily backup found in $BACKUP_DIR." >&2
    exit 1
fi
readonly latest_dump
readonly dump_name="${latest_dump##*/}"

# A stale newest dump means the daily backups stopped.
if [[ -z "$(find "$latest_dump" -mtime "-$MAX_DUMP_AGE_DAYS" -print)" ]]; then
    echo "The newest daily backup is older than $MAX_DUMP_AGE_DAYS days: $dump_name" >&2
    exit 1
fi

read -r _ listed_name <"${latest_dump}.sha256"
if [[ "$listed_name" != "$dump_name" ]]; then
    echo "The checksum file does not describe $dump_name." >&2
    exit 1
fi
(cd -- "$BACKUP_DIR" && sha256sum --check --quiet -- "${dump_name}.sha256")

available_kib="$(df --output=avail -k "$DATA_DIR" | tail -n 1 | tr -d ' ')"
if [[ ! "$available_kib" =~ ^[0-9]+$ ]] || ((available_kib < MIN_FREE_KIB)); then
    echo "Refusing restore test: less than 1 GiB is available for the database." >&2
    exit 1
fi

# Row counts straight from the dump: each COPY block holds one line per row.
dump_counts="$(mktemp)"
readonly dump_counts
pg_restore --data-only --file=- "$latest_dump" | awk '
    /^COPY / && !in_copy { table = $2; rows = 0; in_copy = 1; next }
    in_copy && $0 == "\\." { print table, rows; in_copy = 0; next }
    in_copy { rows++ }
' >"$dump_counts"

dump_version="$(
    pg_restore --data-only --table=alembic_version --file=- "$latest_dump" | awk '
        /^COPY / && !done { in_copy = 1; next }
        in_copy && $0 == "\\." { in_copy = 0; done = 1; next }
        in_copy { print }
    '
)"
if [[ -z "$dump_version" || "$dump_version" == *$'\n'* ]]; then
    echo "The dump does not hold exactly one alembic version." >&2
    exit 1
fi

for table in "${KEY_TABLES[@]}"; do
    if ! grep -q "^public\.$table " "$dump_counts"; then
        echo "The dump has no data section for $table." >&2
        exit 1
    fi
done
if grep -q '^public\.verification_links 0$' "$dump_counts"; then
    echo "The dump holds no rows in verification_links." >&2
    exit 1
fi

# Leftovers from an interrupted run are removed first; the trap removes this run's copy.
drop_restore_database
restore_started=true
createdb --template=template0 "$RESTORE_DATABASE"
psql_test --command="REVOKE ALL ON DATABASE $RESTORE_DATABASE FROM PUBLIC"
if [[ "$(psql_test --command="SELECT current_database()")" != "$RESTORE_DATABASE" ]]; then
    echo "Refusing restore test: not connected to $RESTORE_DATABASE." >&2
    exit 1
fi

pg_restore --dbname="$RESTORE_DATABASE" --no-owner --no-privileges \
    --exit-on-error --single-transaction "$latest_dump"

mismatches=0
tables=0
while read -r table expected <&3; do
    if [[ ! "$table" =~ $TABLE_NAME_PATTERN ]]; then
        echo "Unexpected table name in the dump: $table" >&2
        exit 1
    fi
    actual="$(psql_test --command="SELECT count(*) FROM $table")"
    tables=$((tables + 1))
    if [[ "$actual" != "$expected" ]]; then
        echo "Row count differs for $table: dump $expected, restored $actual" >&2
        mismatches=$((mismatches + 1))
    fi
done 3<"$dump_counts"

restored_version="$(psql_test --command="SELECT version_num FROM alembic_version")"
if [[ "$restored_version" != "$dump_version" ]]; then
    echo "Alembic version differs: dump $dump_version, restored $restored_version" >&2
    exit 1
fi
if ((mismatches > 0)); then
    echo "Tables with a different row count: $mismatches" >&2
    exit 1
fi

drop_restore_database
remaining="$(
    psql --dbname=postgres --no-psqlrc --no-align --tuples-only --quiet --set=ON_ERROR_STOP=1 \
        --command="SELECT count(*) FROM pg_database WHERE datname = '$RESTORE_DATABASE'"
)"
if [[ "$remaining" != "0" ]]; then
    echo "The test database $RESTORE_DATABASE was not removed." >&2
    exit 1
fi

echo "Restore test passed: $dump_name"
echo "Tables compared: $tables, alembic version: $dump_version"
for table in "${KEY_TABLES[@]}"; do
    echo "Rows in $table: $(grep "^public\.$table " "$dump_counts" | cut -d ' ' -f 2)"
done
