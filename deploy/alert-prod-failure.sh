#!/usr/bin/env bash
set -Eeuo pipefail

umask 0077

readonly UNIT="${1:-}"
readonly WEBHOOK_PATTERN='^https://(discord\.com|discordapp\.com)/api/webhooks/[0-9]+/[A-Za-z0-9_-]+$'
readonly WEBHOOK_URL="${DISCORD_ALERT_WEBHOOK_URL:-}"
readonly USER_ID_PATTERN='^[0-9]{17,20}$'
readonly MENTION_USER_ID="${DISCORD_ALERT_MENTION_USER_ID:-}"

case "$UNIT" in
    moon-poro-prod-backup.service) title="backup bazy się nie udał" ;;
    moon-poro-prod-pre-deploy-backup.service) title="backup przed wdrożeniem się nie udał" ;;
    moon-poro-prod-offsite-backup.service) title="kopia w Cloud Storage się nie udała" ;;
    moon-poro-prod-restore-test.service) title="test odtworzenia backupu się nie udał" ;;
    test) title="testowe powiadomienie, nic się nie stało" ;;
    *)
        echo "Usage: $0 {moon-poro-prod-*.service|test}" >&2
        exit 1
        ;;
esac

# The address is never printed: anyone holding it can post on the channel.
if [[ ! "$WEBHOOK_URL" =~ $WEBHOOK_PATTERN ]]; then
    echo "DISCORD_ALERT_WEBHOOK_URL is missing or is not a Discord webhook address." >&2
    exit 1
fi

now="$(date -u '+%Y-%m-%d %H:%M UTC')"
if [[ "$UNIT" == test ]]; then
    message="⚠️ Moon Poro: $title"$'\n'"Tak wyglądają powiadomienia o błędach backupu."$'\n'"Czas: $now"
else
    message="⚠️ Moon Poro: $title"$'\n'"Jednostka: \`$UNIT\`"$'\n'"Czas: $now"
    message+=$'\n'"Sprawdź: \`journalctl -u $UNIT -n 50\`"
fi

# A bad ID only drops the ping; the alert itself still goes out.
mention="$MENTION_USER_ID"
if [[ -n "$mention" && ! "$mention" =~ $USER_ID_PATTERN ]]; then
    echo "DISCORD_ALERT_MENTION_USER_ID is not a Discord user ID; sending without a ping." >&2
    mention=""
fi

# Only the one configured person can be pinged, never @everyone or a role.
payload="$(
    python3 - "$message" "$mention" <<'PY'
import json
import sys

message, mention = sys.argv[1], sys.argv[2]
if mention:
    payload = {"content": f"<@{mention}> {message}", "allowed_mentions": {"users": [mention]}}
else:
    payload = {"content": message, "allowed_mentions": {"parse": []}}
print(json.dumps(payload))
PY
)"

# curl reads the address from a pipe, so it never appears on a command line.
curl --fail --silent --show-error --max-time 20 --retry 3 --retry-delay 5 \
    --config <(printf 'url = "%s"\n' "$WEBHOOK_URL") \
    --header "Content-Type: application/json" \
    --data-binary "$payload" \
    --output /dev/null

echo "Discord alert sent for $UNIT."
