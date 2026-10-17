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
        assert "handle_errors 404" in source
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
