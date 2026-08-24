#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TOOL_ENV="/opt/moon-poro-deploy-tools"
readonly RUNTIME_DIR="/opt/moon-poro-prod-runtime"
readonly BACKUP_DIR="/var/backups/moon-poro-prod"

if ((EUID != 0)); then
    echo "Run this installer as root." >&2
    exit 1
fi
if ! getent passwd postgres >/dev/null; then
    echo "The postgres system user does not exist." >&2
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
    "$SCRIPT_DIR/deploy-prod.sh" /usr/local/sbin/deploy-moon-poro-prod
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-backup.service" \
    /etc/systemd/system/moon-poro-prod-backup.service
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-pre-deploy-backup.service" \
    /etc/systemd/system/moon-poro-prod-pre-deploy-backup.service
install -o root -g root -m 0644 \
    "$SCRIPT_DIR/moon-poro-prod-backup.timer" \
    /etc/systemd/system/moon-poro-prod-backup.timer

systemctl daemon-reload
systemctl enable --now moon-poro-prod-backup.timer

echo "Production deployment command: sudo /usr/local/sbin/deploy-moon-poro-prod"
echo "Manual verified backup: sudo systemctl start moon-poro-prod-backup.service"
echo "Daily backups remain for 90 days in $BACKUP_DIR/daily."
echo "Pre-deploy backups also remain for 90 days in $BACKUP_DIR/pre-deploy."
