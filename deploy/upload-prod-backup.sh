#!/usr/bin/env bash
set -Eeuo pipefail

umask 0077

readonly BACKUP_ROOT="/var/backups/moon-poro-prod"
readonly MAX_AGE_DAYS=7
readonly BACKUP_NAME_PATTERN='^moon_poro_prod-[0-9]{8}T[0-9]{6}Z\.dump$'
readonly BUCKET_PATTERN='^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$'
readonly METADATA_TOKEN_URL="http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
readonly BUCKET="${BACKUP_GCS_BUCKET:-}"

if [[ ! "$BUCKET" =~ $BUCKET_PATTERN ]]; then
    echo "BACKUP_GCS_BUCKET is missing or is not a valid bucket name." >&2
    exit 1
fi

if [[ "$(id -un)" != "postgres" ]]; then
    echo "The backup upload must run as the postgres system user." >&2
    exit 1
fi

exec 9>"$BACKUP_ROOT/.offsite.lock"
if ! flock -w 600 9; then
    echo "Another backup upload is still running." >&2
    exit 1
fi

# The token never appears on a command line; curl reads the header from a pipe.
access_token() {
    curl --fail --silent --show-error --max-time 10 \
        --header "Metadata-Flavor: Google" "$METADATA_TOKEN_URL" |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}

md5_base64() {
    python3 - "$1" <<'PY'
import base64
import hashlib
import sys

digest = hashlib.md5(usedforsecurity=False)
with open(sys.argv[1], "rb") as source:
    for chunk in iter(lambda: source.read(1 << 20), b""):
        digest.update(chunk)
print(base64.b64encode(digest.digest()).decode())
PY
}

# ifGenerationMatch=0 never overwrites an object, and the bucket role only allows creating them.
upload_object() {
    local path="$1"
    local object_name="$2"
    local response status remote_md5

    response="$(mktemp)"
    if ! status="$(
        curl --silent --show-error --max-time 300 \
            --output "$response" --write-out '%{http_code}' \
            --header @<(printf 'Authorization: Bearer %s\n' "$token") \
            --header "Content-Type: application/octet-stream" \
            --data-binary "@$path" \
            "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&ifGenerationMatch=0&name=${object_name//\//%2F}"
    )"; then
        rm -f -- "$response"
        echo "Upload request failed: $object_name" >&2
        return 1
    fi

    case "$status" in
        200)
            remote_md5="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["md5Hash"])' "$response")"
            rm -f -- "$response"
            if [[ "$remote_md5" != "$(md5_base64 "$path")" ]]; then
                echo "Cloud Storage checksum does not match the local file: $object_name" >&2
                return 1
            fi
            ;;
        412)
            rm -f -- "$response"
            echo "Already in gs://$BUCKET, not replaced: $object_name"
            ;;
        *)
            echo "Cloud Storage returned HTTP $status for $object_name:" >&2
            head -c 500 -- "$response" >&2
            echo >&2
            rm -f -- "$response"
            return 1
            ;;
    esac
}

token="$(access_token)"
failures=0

for kind in daily pre-deploy; do
    backup_dir="$BACKUP_ROOT/$kind"
    while IFS= read -r -d '' dump; do
        name="${dump##*/}"
        if [[ ! "$name" =~ $BACKUP_NAME_PATTERN || -e "${dump}.uploaded" ]]; then
            continue
        fi
        if ! (cd -- "$backup_dir" && sha256sum --check --quiet -- "${name}.sha256"); then
            echo "Local checksum failed, not uploading: $kind/$name" >&2
            failures=$((failures + 1))
            continue
        fi
        if upload_object "$dump" "$kind/$name" &&
            upload_object "${dump}.sha256" "$kind/${name}.sha256"; then
            touch -- "${dump}.uploaded"
            echo "Copied $kind/$name to gs://$BUCKET"
        else
            failures=$((failures + 1))
        fi
    done < <(
        find "$backup_dir" -maxdepth 1 -type f -name "moon_poro_prod-*.dump" \
            -mtime "-$MAX_AGE_DAYS" -print0 | sort -z
    )
done

if ((failures > 0)); then
    echo "Backups not copied to Cloud Storage: $failures" >&2
    exit 1
fi
