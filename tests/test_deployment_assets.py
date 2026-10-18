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
    for filename in ("Caddyfile", "Caddyfile.web-http", "Caddyfile.web-pre-rso"):
        source = (ROOT / "deploy" / filename).read_text(encoding="utf-8")
        assert "try_files {path} {path}/index.html =404" in source
        # Status-code arguments need Caddy 2.8; the distro package is 2.6.
        assert "handle_errors 404" not in source
        assert "@not_found expression `{err.status_code} == 404`" in source
        assert "rewrite * /404.html" in source
    production = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
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
