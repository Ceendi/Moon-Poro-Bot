import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_legacy_archive_report_is_read_only_and_aggregated() -> None:
    source = (ROOT / "deploy/inspect-legacy-verification-archive.psql").read_text(encoding="utf-8")
    assert "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;" in source
    assert "to_regclass('public.legacy_zweryfikowani')" in source
    assert "\\if :archive_exists" in source
    assert "count(*)" in source
    statements = "\n".join(line for line in source.splitlines() if not line.startswith("--"))
    for forbidden in ("DELETE ", "DROP ", "ALTER ", "UPDATE ", "INSERT ", "TRUNCATE "):
        assert forbidden not in statements.upper()


def test_static_404_fallback_uses_error_handler_without_overriding_rso_headers() -> None:
    production = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    assert "try_files {path} {path}/index.html =404" in production
    # Status-code arguments need Caddy 2.8; the distro package is 2.6.
    assert "handle_errors 404" not in production
    assert "@not_found expression `{err.status_code} == 404`" in production
    assert "rewrite * /404.html" in production
    assert "handle /verify/*" in production
    assert "handle /oauth2/callback" in production
    assert production.count("import moon_poro_static_headers") == 2


def test_caddy_blocks_exact_public_readiness_and_keeps_cheap_health_proxy() -> None:
    production = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    readiness = production.split("handle /readyz {", 1)[1].split("}", 1)[0]
    assert "respond 404" in readiness
    assert "reverse_proxy" not in readiness
    assert "handle /readyz*" not in production
    for route in ("/healthz", "/verify/*", "/oauth2/callback"):
        handler = production.split(f"handle {route} {{", 1)[1].split("}", 1)[0]
        assert "reverse_proxy 127.0.0.1:8080" in handler
        assert "import moon_poro_static_headers" not in handler


def test_production_rso_unit_runs_the_deployed_checkout() -> None:
    unit = (ROOT / "deploy/moon-poro-prod-rso.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/moon-poro-prod" in unit
    assert "ExecStart=/opt/moon-poro-prod/.venv/bin/python /opt/moon-poro-prod/rso_main.py" in unit
    assert "EnvironmentFile=/etc/moon-poro/prod-rso.env" in unit
    assert "User=moon-poro-rso" in unit
    installer = (ROOT / "deploy/install-prod-automation.sh").read_text(encoding="utf-8")
    assert "moon-poro-prod-rso.service" in installer
    assert "enable --now moon-poro-prod-rso" not in installer.split("echo", 1)[0]


def test_production_bot_unit_runs_the_deployed_checkout() -> None:
    unit = (ROOT / "deploy/moon-poro-prod.service").read_text(encoding="utf-8")
    assert "User=moon-poro-prod\n" in unit
    assert "Group=moon-poro-prod\n" in unit
    assert "WorkingDirectory=/opt/moon-poro-prod\n" in unit
    assert "ExecStart=/opt/moon-poro-prod/.venv/bin/python /opt/moon-poro-prod/main.py" in unit
    assert "EnvironmentFile=/etc/moon-poro/prod-bot.env" in unit
    assert "Requires=postgresql-moon-poro-prod.service" in unit
    assert "After=network-online.target postgresql-moon-poro-prod.service" in unit
    assert "ReadWritePaths=/var/log/moon-poro-prod" in unit
    # Settings come from the env file; the unit itself carries no secrets.
    for line in unit.splitlines():
        if line.startswith("Environment="):
            assert line.split("=", 2)[1] in {
                "MOON_PORO_LOG_FILE",
                "PYTHONDONTWRITEBYTECODE",
                "PYTHONUNBUFFERED",
            }
    installer = (ROOT / "deploy/install-prod-automation.sh").read_text(encoding="utf-8")
    assert "/etc/systemd/system/moon-poro-prod.service" in installer
    assert "systemctl enable moon-poro-prod.service" in installer
    # Reinstalling must not restart the running bot; echo lines only print hints.
    commands = "\n".join(
        line for line in installer.splitlines() if not line.lstrip().startswith("echo")
    )
    for command in ("restart", "start", "stop", "enable --now"):
        assert f"systemctl {command} moon-poro-prod.service" not in commands


def test_production_backups_require_the_renamed_cluster_unit() -> None:
    cluster = (ROOT / "deploy/postgresql-moon-poro-prod.service").read_text(encoding="utf-8")
    assert "AssertPathExists=/etc/postgresql/18/moon-poro-prod/postgresql.conf" in cluster
    assert "PIDFile=/run/postgresql/18-moon-poro-prod.pid" in cluster
    for action in ("18 moon-poro-prod start", "-m fast 18 moon-poro-prod stop"):
        assert f"--skip-systemctl-redirect {action}\n" in cluster
    for name in ("moon-poro-prod-backup.service", "moon-poro-prod-pre-deploy-backup.service"):
        unit = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "After=postgresql-moon-poro-prod.service" in unit
        assert "Requires=postgresql-moon-poro-prod.service" in unit
    installer = (ROOT / "deploy/install-prod-automation.sh").read_text(encoding="utf-8")
    check = installer.index('if [[ ! -f "$CLUSTER_CONFIG" ]]')
    assert check < installer.index("/etc/systemd/system/postgresql-moon-poro-prod.service")
    for path in (ROOT / "deploy").iterdir():
        if path.is_file():
            assert "moon-poro-test" not in path.read_text(encoding="utf-8"), path.name


def test_backups_stay_offline_and_trigger_the_offsite_copy() -> None:
    for name in ("moon-poro-prod-backup.service", "moon-poro-prod-pre-deploy-backup.service"):
        unit = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "RestrictAddressFamilies=AF_UNIX\n" in unit
        assert "OnSuccess=moon-poro-prod-offsite-backup.service" in unit
    offsite = (ROOT / "deploy/moon-poro-prod-offsite-backup.service").read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/moon-poro/prod-backup-offsite.env" in offsite
    assert "EnvironmentFile=/etc/moon-poro/prod-backup-offsite.env" in offsite
    assert "User=postgres" in offsite
    assert "ProtectProc=invisible" in offsite
    assert "ExecStart=/usr/local/sbin/upload-moon-poro-prod-backup" in offsite
    installer = (ROOT / "deploy/install-prod-automation.sh").read_text(encoding="utf-8")
    assert "/usr/local/sbin/upload-moon-poro-prod-backup" in installer
    assert "/etc/systemd/system/moon-poro-prod-offsite-backup.service" in installer
    assert "enable --now moon-poro-prod-offsite-backup" not in installer


def test_offsite_upload_never_replaces_objects_or_exposes_the_token() -> None:
    script = (ROOT / "deploy/upload-prod-backup.sh").read_text(encoding="utf-8")
    assert "ifGenerationMatch=0" in script
    assert "sha256sum --check" in script
    assert '"md5Hash"' in script
    assert "--header @<(printf 'Authorization: Bearer %s\\n' \"$token\")" in script
    assert script.count("Authorization: Bearer") == 1
    assert "set -x" not in script
    pruning = (ROOT / "deploy/backup-prod-db.sh").read_text(encoding="utf-8")
    assert 'rm -f -- "${old_dump}.uploaded"' in pruning


def test_production_deploy_stops_rso_before_migrations_and_restarts_it_after_the_bot() -> None:
    script = (ROOT / "deploy/deploy-prod.sh").read_text(encoding="utf-8")
    stop_rso = script.index('systemctl stop "$RSO_SERVICE"')
    migrate = script.index("-m moon_poro.migrate")
    start_bot = script.index('systemctl start "$SERVICE"')
    start_rso = script.index('systemctl start "$RSO_SERVICE"')
    assert stop_rso < migrate < start_bot < start_rso


def test_bootstrap_validates_caddyfile_before_installing_it() -> None:
    script = (ROOT / "deploy/bootstrap-web.sh").read_text(encoding="utf-8")
    validate = script.index('caddy validate --config "$CADDY_SOURCE"')
    install = script.index('"$CADDY_SOURCE" /etc/caddy/Caddyfile')
    assert validate < install


def test_monthly_restore_test_runs_offline_and_keeps_backups_read_only() -> None:
    unit = (ROOT / "deploy/moon-poro-prod-restore-test.service").read_text(encoding="utf-8")
    assert "User=postgres\n" in unit
    assert "Requires=postgresql-moon-poro-prod.service" in unit
    assert "ExecStart=/usr/local/sbin/restore-test-moon-poro-prod-db\n" in unit
    assert "RestrictAddressFamilies=AF_UNIX\n" in unit
    assert "MemoryMax=128M" in unit
    assert "ReadWritePaths=" not in unit
    timer = (ROOT / "deploy/moon-poro-prod-restore-test.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-01 05:30:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=moon-poro-prod-restore-test.service" in timer
    installer = (ROOT / "deploy/install-prod-automation.sh").read_text(encoding="utf-8")
    assert "/usr/local/sbin/restore-test-moon-poro-prod-db" in installer
    assert "/etc/systemd/system/moon-poro-prod-restore-test.service" in installer
    assert "systemctl enable --now moon-poro-prod-restore-test.timer" in installer


def test_restore_test_never_touches_the_production_database() -> None:
    script = (ROOT / "deploy/restore-test-prod-db.sh").read_text(encoding="utf-8")
    assert 'readonly RESTORE_DATABASE="moon_poro_restore_test"' in script
    assert 'if [[ "$RESTORE_DATABASE" == "$PROD_DATABASE" ]]' in script
    assert 'export PGCLUSTER="18/moon-poro-prod"' in script
    assert "export PGPORT=5433" in script
    assert "maintenance_work_mem=16MB" in script
    # Only the test database and the maintenance database are ever named as targets.
    targets = re.findall(r"--dbname=(\S+)", script)
    assert targets
    assert set(targets) == {'"$RESTORE_DATABASE"', "postgres"}
    for command in ("createdb", "dropdb"):
        for line in script.splitlines():
            if line.lstrip().startswith(command):
                assert line.rstrip().endswith('"$RESTORE_DATABASE"'), line
    for line in script.splitlines():
        if "moon_poro_prod" in line:
            assert line.startswith(("readonly PROD_DATABASE=", "readonly BACKUP_NAME_PATTERN=")), (
                line
            )
    assert "sha256sum --check" in script
    assert "--exit-on-error --single-transaction" in script
    assert "--no-owner --no-privileges" in script
    assert "trap cleanup EXIT" in script
    assert "SELECT version_num FROM alembic_version" in script
    for table in ("verification_links", "warnings", "moderation_stats", "guild_features"):
        assert table in script


def test_backup_units_send_an_alert_on_failure() -> None:
    for name in (
        "moon-poro-prod-backup.service",
        "moon-poro-prod-pre-deploy-backup.service",
        "moon-poro-prod-offsite-backup.service",
        "moon-poro-prod-restore-test.service",
    ):
        unit = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "OnFailure=moon-poro-prod-alert@%n.service\n" in unit, name
        unit_section = unit.split("[Service]", 1)[0]
        assert "OnFailure=" in unit_section, name
    alert = (ROOT / "deploy/moon-poro-prod-alert@.service").read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/moon-poro/prod-alert.env" in alert
    assert "EnvironmentFile=/etc/moon-poro/prod-alert.env" in alert
    assert "DynamicUser=true" in alert
    assert "ExecStart=/usr/local/sbin/alert-moon-poro-prod-failure %i\n" in alert
    assert "Environment=" not in alert.replace("EnvironmentFile=", "")
    installer = (ROOT / "deploy/install-prod-automation.sh").read_text(encoding="utf-8")
    assert "/usr/local/sbin/alert-moon-poro-prod-failure" in installer
    assert "/etc/systemd/system/moon-poro-prod-alert@.service" in installer
    assert "enable --now moon-poro-prod-alert" not in installer


def test_failure_alert_keeps_the_webhook_off_command_lines_and_pings_one_person() -> None:
    script = (ROOT / "deploy/alert-prod-failure.sh").read_text(encoding="utf-8")
    assert r"^https://(discord\.com|discordapp\.com)/api/webhooks/" in script
    assert r"""--config <(printf 'url = "%s"\n' "$WEBHOOK_URL")""" in script
    assert script.count("$WEBHOOK_URL") == 2
    # Without an ID nobody is pinged; with one, only that person can be.
    assert "readonly USER_ID_PATTERN='^[0-9]{17,20}$'" in script
    assert '"allowed_mentions": {"parse": []}' in script
    assert '"allowed_mentions": {"users": [mention]}' in script
    assert '"parse": ["' not in script
    assert "set -x" not in script
    for unit in (
        "moon-poro-prod-backup.service",
        "moon-poro-prod-pre-deploy-backup.service",
        "moon-poro-prod-offsite-backup.service",
        "moon-poro-prod-restore-test.service",
    ):
        assert f"    {unit}) title=" in script
