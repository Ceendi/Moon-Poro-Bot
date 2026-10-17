import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import ClientResponse, StreamReader
from aiohttp.base_protocol import BaseProtocol
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from moon_poro.database import Database
from moon_poro.models import Base, VerificationSessionStatus
from moon_poro.rso_web import (
    _BACKGROUND_TASKS,
    BROWSER_COOKIE,
    HTTP_CLIENT_KEY,
    MAX_RIOT_RESPONSE_BYTES,
    NEW_LINK_HINT,
    REPOSITORY_KEY,
    RSO_CLIENT_KEY,
    RequestRateLimiter,
    RiotIdentity,
    RiotRSOClient,
    RiotRSOError,
    _read_riot_json,
    _result_for_record,
    create_app,
)
from moon_poro.settings import RSOSettings
from moon_poro.verification_sessions import SessionExpired


def make_rso_settings(**overrides: object) -> RSOSettings:
    values: dict[str, object] = {
        "postgres_user": "rso",
        "postgres_password": "db-secret",  # pragma: allowlist secret
        "postgres_host": "127.0.0.1",
        "postgres_db": "moon_poro",
        "guild_id": 123,
        "rso_client_id": "moon-poro-client",
        "rso_client_auth_method": "private_key_jwt",
        "rso_client_assertion": "signed-client-assertion",  # pragma: allowlist secret
        "rso_public_base_url": "https://bot.example.com",
    }
    values.update(overrides)
    return RSOSettings(_env_file=None, **values)


class SQLiteTestDatabase:
    def __init__(self, session_factory, engine) -> None:
        self.session_factory = session_factory
        self.engine = engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()


@pytest_asyncio.fixture
async def rso_test_client() -> AsyncIterator[TestClient]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database = cast(Database, SQLiteTestDatabase(factory, engine))
    settings = make_rso_settings(rso_public_base_url="http://localhost:8080")
    client = TestClient(TestServer(create_app(settings, database)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def test_authorization_url_uses_exact_callback_and_minimal_scopes() -> None:
    client = RiotRSOClient(make_rso_settings(), Mock())

    parts = urlsplit(client.authorization_url("state-value"))
    query = parse_qs(parts.query)

    assert parts.netloc == "auth.riotgames.com"
    assert parts.path == "/authorize"
    assert query["redirect_uri"] == ["https://bot.example.com/oauth2/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid cpid"]
    assert query["state"] == ["state-value"]
    assert "offline_access" not in query["scope"][0]


def test_rso_settings_reject_refresh_token_scope() -> None:
    with pytest.raises(ValidationError, match="offline_access is not used"):
        make_rso_settings(rso_scope="openid cpid offline_access")


def test_rso_settings_require_credential_for_selected_auth_method() -> None:
    with pytest.raises(ValidationError, match="requires RSO_CLIENT_SECRET"):
        make_rso_settings(
            rso_client_auth_method="client_secret_basic",
            rso_client_secret=None,
        )


def test_rso_settings_reject_platform_without_discord_roles() -> None:
    with pytest.raises(ValidationError, match="without configured Discord roles"):
        make_rso_settings(rso_allowed_platforms=["EUN1", "KR"])


async def test_identity_combines_userinfo_platform_and_account() -> None:
    client = RiotRSOClient(make_rso_settings(), Mock())
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"sub": "opaque-player", "cpid": "eun1"},
            {"puuid": "player-puuid", "gameName": "Moon", "tagLine": "EUNE"},
        ]
    )

    identity = await client.get_identity("temporary-access-token")

    assert identity.puuid == "player-puuid"
    assert identity.platform == "EUN1"
    assert identity.game_name == "Moon"
    assert (
        client._get_json.await_args_list[1]
        .args[0]
        .startswith(  # type: ignore[attr-defined]
            "https://europe.api.riotgames.com/"
        )
    )


async def test_identity_uses_americas_cluster_for_na() -> None:
    client = RiotRSOClient(make_rso_settings(), Mock())
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"sub": "opaque-player", "cpid": "na1"},
            {"puuid": "player-puuid", "gameName": "Moon", "tagLine": "NA"},
        ]
    )

    identity = await client.get_identity("temporary-access-token")

    assert identity.platform == "NA1"
    assert (
        client._get_json.await_args_list[1]
        .args[0]
        .startswith("https://americas.api.riotgames.com/")
    )


async def test_identity_rejects_unconfigured_platform() -> None:
    client = RiotRSOClient(make_rso_settings(), Mock())
    client._get_json = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"sub": "opaque-player", "cpid": "kr"},
            {"puuid": "player-puuid", "gameName": "Moon", "tagLine": "KR"},
        ]
    )

    with pytest.raises(RiotRSOError, match="UNSUPPORTED_PLATFORM"):
        await client.get_identity("temporary-access-token")


def result_record(status: str, **overrides: object) -> SimpleNamespace:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "status": status,
        "error_code": None,
        "guild_id": 123,
        "riot_game_name": None,
        "riot_tag_line": None,
        "expires_at": now + timedelta(minutes=10),
        "updated_at": now,
        "verification_link_created_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_completed_result_escapes_riot_id() -> None:
    record = result_record(
        VerificationSessionStatus.COMPLETED.value,
        riot_game_name="<script>",
        riot_tag_line="tag&line",
    )

    response = _result_for_record(record)

    assert "<script>" not in response.text
    assert "&lt;script&gt;#tag&amp;line" in response.text
    assert "https://discord.com/channels/123" in response.text


def test_processing_riot_does_not_claim_identity_has_been_confirmed() -> None:
    response = _result_for_record(result_record("PROCESSING_RIOT"))
    assert "Potwierdzamy logowanie w Riot" in response.text
    assert "Riot potwierdził konto" not in response.text
    assert response.headers["Refresh"] == "2"


@pytest.mark.parametrize("status", ["VERIFIED_PENDING_DISCORD", "APPLYING_DISCORD"])
def test_pending_discord_does_not_claim_roles_are_still_being_applied(status: str) -> None:
    response = _result_for_record(result_record(status))

    assert "Kończymy weryfikację na Discordzie" in response.text
    assert "Bot nadaje teraz role" not in response.text
    assert response.headers["Refresh"] == "2"


def test_paused_discord_result_requests_explicit_resume_without_refresh() -> None:
    response = _result_for_record(
        result_record(VerificationSessionStatus.DISCORD_RETRY_PAUSED.value)
    )

    assert "Weryfikacja wymaga pomocy" in response.text
    assert "Kliknij ponownie „Zweryfikuj konto” na Discordzie" in response.text
    assert "skontaktuj się z moderacją" in response.text
    assert "Refresh" not in response.headers
    assert "Konto połączone" not in response.text


def test_rate_limiter_rejects_request_after_limit() -> None:
    limiter = RequestRateLimiter(limit=2, window_seconds=60)

    assert limiter.allowed("127.0.0.1")
    assert limiter.allowed("127.0.0.1")
    assert not limiter.allowed("127.0.0.1")


def test_rate_limiter_shares_ipv6_64_but_keeps_other_networks_independent() -> None:
    limiter = RequestRateLimiter(limit=2)

    assert limiter.allowed("2001:db8:1234:5678::1")
    assert limiter.allowed("2001:0db8:1234:5678:ffff:ffff:ffff:ffff")
    assert not limiter.allowed("2001:db8:1234:5678::2")
    assert limiter.allowed("2001:db8:1234:5679::1")
    assert len(limiter._clients) == 2


def test_rate_limiter_maps_ipv4_and_rejects_invalid_addresses_without_allocating() -> None:
    limiter = RequestRateLimiter(limit=1)

    assert limiter.allowed("192.0.2.5")
    assert not limiter.allowed("::ffff:c000:205")
    assert not limiter.allowed("::ffff:192.0.2.5")
    assert limiter.allowed("192.0.2.6")
    for client in ("unknown", "invalid-address", "x" * 1000):
        assert not limiter.allowed(client)
    assert len(limiter._clients) == 2


def test_rate_limiter_admits_new_clients_at_capacity_by_evicting_oldest_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("moon_poro.rso_web.time.monotonic", lambda: clock[0])
    limiter = RequestRateLimiter(limit=1, window_seconds=60)
    for index in range(4096):
        assert limiter.allowed(f"10.0.{index // 256}.{index % 256}")
    clock[0] = 101.0
    assert not limiter.allowed("10.0.15.255")

    # A flood of fresh addresses never locks out the next new client.
    for index in range(5000):
        assert limiter.allowed(f"2001:db8:{index:x}::1")
    assert limiter.allowed("192.0.2.1")
    assert len(limiter._clients) == 4096
    # Known clients keep counting while they are in the table.
    assert not limiter.allowed("192.0.2.1")
    assert not limiter.allowed("2001:db8:1387::5")


def test_rate_limiter_evicts_oldest_window_first_and_keeps_recent_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("moon_poro.rso_web.time.monotonic", lambda: clock[0])
    limiter = RequestRateLimiter(limit=1, window_seconds=60, max_clients=3)
    assert limiter.allowed("192.0.2.1")
    clock[0] = 101.0
    assert limiter.allowed("192.0.2.2")
    clock[0] = 102.0
    assert limiter.allowed("192.0.2.3")
    assert not limiter.allowed("192.0.2.2")

    clock[0] = 103.0
    assert limiter.allowed("192.0.2.4")
    assert list(limiter._clients) == ["192.0.2.2", "192.0.2.3", "192.0.2.4"]
    assert not limiter.allowed("192.0.2.2")
    assert not limiter.allowed("192.0.2.3")


def test_rate_limiter_resets_window_and_prunes_expired_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("moon_poro.rso_web.time.monotonic", lambda: clock[0])
    limiter = RequestRateLimiter(limit=1, window_seconds=60)
    assert limiter.allowed("192.0.2.1")
    clock[0] = 130.0
    assert limiter.allowed("192.0.2.2")
    clock[0] = 159.999
    assert not limiter.allowed("192.0.2.1")
    clock[0] = 160.0
    assert limiter.allowed("192.0.2.1")
    # The reset bucket moved behind the younger one, so pruning stays ordered.
    assert list(limiter._clients) == ["192.0.2.2", "192.0.2.1"]
    clock[0] = 190.0
    assert not limiter.allowed("192.0.2.1")
    assert list(limiter._clients) == ["192.0.2.1"]
    clock[0] = 1000.0
    assert limiter.allowed("192.0.2.9")
    assert list(limiter._clients) == ["192.0.2.9"]


async def test_health_readiness_and_security_headers(rso_test_client: TestClient) -> None:
    headers = {"Host": "localhost:8080"}

    health = await rso_test_client.get("/healthz", headers=headers)
    ready = await rso_test_client.get("/readyz", headers=headers)
    invalid_host = await rso_test_client.get("/healthz", headers={"Host": "evil.example"})

    assert await health.json() == {"status": "ok"}
    assert await ready.json() == {"status": "ready"}
    assert health.headers["X-Frame-Options"] == "DENY"
    assert health.headers["Cache-Control"] == "no-store, max-age=0"
    # no-referrer makes browsers send Origin: null on ordinary form POSTs.
    # same-origin preserves local form checks without leaking tokens to Riot.
    assert health.headers["Referrer-Policy"] == "same-origin"
    assert (
        "form-action 'self' https://auth.riotgames.com https://authenticate.riotgames.com;"
        in health.headers["Content-Security-Policy"]
    )
    assert "img-src 'self' data:;" in health.headers["Content-Security-Policy"]
    assert "font-src 'self';" in health.headers["Content-Security-Policy"]
    assert invalid_host.status == 400
    assert await invalid_host.text() == "Nieprawidłowy adres."


async def test_request_limit_returns_a_clear_polish_message(
    rso_test_client: TestClient,
) -> None:
    headers = {"Host": "localhost:8080"}

    responses = [
        await rso_test_client.get("/healthz", headers=headers) for _request_number in range(61)
    ]

    assert responses[-1].status == 429
    assert await responses[-1].text() == (
        "Za dużo prób w krótkim czasie. Poczekaj chwilę, strona odświeży się sama."
    )
    assert responses[-1].headers["Refresh"] == "10"
    assert responses[-1].headers["Cache-Control"] == "no-store, max-age=0"
    ready = await rso_test_client.get("/readyz", headers=headers)
    assert ready.status == 429


@pytest.mark.parametrize(
    ("username", "guild_name"),
    [(None, None), ('<img src=x onerror="alert(1)">&', "<script>guild</script>&")],
    ids=["legacy-id-fallback", "escaped-names"],
)
async def test_landing_identifies_discord_target_before_riot_button(
    rso_test_client: TestClient, username: str | None, guild_name: str | None
) -> None:
    repository = rso_test_client.server.app[REPOSITORY_KEY]
    created = await repository.create(
        guild_id=123,
        user_id=456,
        ttl_seconds=600,
        discord_username=username,
        discord_guild_name=guild_name,
    )
    response = await rso_test_client.get(
        f"/verify/start/{created.token}", headers={"Host": "localhost:8080"}
    )
    document = await response.text()

    assert response.status == 200
    assert "Łączysz konto Riot z kontem Discord" in document
    assert "<strong></strong>" not in document
    warning = "Jeśli to nie Twoje konto Discord lub ktoś przesłał Ci ten link, zamknij stronę."
    assert warning in document
    assert "Rozpocznij weryfikację samodzielnie w panelu bota." in document
    assert document.index("Łączysz konto Riot") < document.index("<button")
    assert document.index(warning) < document.index("<button")
    if username is not None:
        assert username not in document and guild_name not in document
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;" in document
        assert "&lt;script&gt;guild&lt;/script&gt;&amp;" in document
        assert "(ID: 456) na serwerze <strong>" in document
        assert "(ID: 123)." in document
    else:
        assert "None" not in document
        assert "z kontem Discord o ID <strong>456</strong>" in document
        assert "na serwerze o ID <strong>123</strong>." in document
    assert 'src="/moon-poro.png"' in document
    assert "/fonts/nunito-latin.woff2" in document


async def test_complete_browser_flow_from_landing_to_result(
    rso_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    headers = {"Host": "localhost:8080"}

    landing = await rso_test_client.get(path, headers=headers)
    untouched = await repository.get_by_start_token(created.token)
    assert landing.status == 200
    landing_html = await landing.text()
    assert "Połącz konto przez Riot" in landing_html
    assert "Riot przekaże nam Twoje Riot ID i region" in landing_html
    assert "Przejdź do logowania Riot" in landing_html
    assert untouched is not None and untouched.status == VerificationSessionStatus.CREATED.value

    start = await rso_test_client.post(
        path,
        headers={**headers, "Origin": "http://localhost:8080"},
        allow_redirects=False,
    )
    authorization = urlsplit(start.headers["Location"])
    state = parse_qs(authorization.query)["state"][0]
    assert authorization.netloc == "auth.riotgames.com"

    exchange_code = AsyncMock(return_value="short-lived-token")
    get_identity = AsyncMock(
        return_value=RiotIdentity(
            puuid="web-flow-puuid",
            game_name="Moon",
            tag_line="EUNE",
            platform="EUN1",
        )
    )
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", exchange_code)
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "get_identity", get_identity)
    callback = await rso_test_client.get(
        f"/oauth2/callback?state={state}&code=authorization-code",
        headers={**headers, "Cookie": f"{BROWSER_COOKIE}={start.cookies[BROWSER_COOKIE].value}"},
        allow_redirects=False,
    )
    assert callback.status == 303
    exchange_code.assert_awaited_once_with("authorization-code")

    pending_page = await rso_test_client.get(
        "/verify/result",
        headers={**headers, "Cookie": f"moon_poro_rso={created.token}"},
    )
    assert pending_page.headers["Refresh"] == "2"
    assert "Riot potwierdził konto" in await pending_page.text()

    pending = await repository.claim_pending()
    assert await repository.complete_discord(
        pending[0].id,
        message_id=987,
        channel_id=654,
    )
    completed_page = await rso_test_client.get(
        "/verify/result",
        headers={**headers, "Cookie": f"moon_poro_rso={created.token}"},
    )
    completed_html = await completed_page.text()
    assert "Konto połączone" in completed_html
    assert "Moon#EUNE" in completed_html


async def test_cancelled_provider_flow_and_reused_link(rso_test_client: TestClient) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=789, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    headers = {"Host": "localhost:8080", "Origin": "http://localhost:8080"}
    start = await rso_test_client.post(path, headers=headers, allow_redirects=False)
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]

    cancelled = await rso_test_client.get(
        f"/oauth2/callback?state={state}&error=access_denied",
        headers={
            "Host": "localhost:8080",
            "Cookie": f"{BROWSER_COOKIE}={start.cookies[BROWSER_COOKIE].value}",
        },
        allow_redirects=False,
    )
    reused = await rso_test_client.post(path, headers=headers, allow_redirects=False)
    result = await rso_test_client.get(
        "/verify/result",
        headers={"Host": "localhost:8080", "Cookie": f"moon_poro_rso={created.token}"},
    )

    assert cancelled.status == 303
    assert reused.status == 200
    assert "Każdy link działa tylko raz" in await reused.text()
    assert "Logowanie anulowane" in await result.text()


async def test_invalid_tokens_and_origin_are_rejected(rso_test_client: TestClient) -> None:
    headers = {"Host": "localhost:8080"}

    invalid_link = await rso_test_client.get("/verify/start/not-a-token", headers=headers)
    invalid_state = await rso_test_client.get(
        "/oauth2/callback?state=bad&code=code", headers=headers
    )
    bad_origin = await rso_test_client.post(
        "/verify/start/" + "a" * 43,
        headers={**headers, "Origin": "https://evil.example"},
    )
    missing_cookie = await rso_test_client.get("/verify/result", headers=headers)

    assert "Nieprawidłowy link" in await invalid_link.text()
    invalid_state_html = await invalid_state.text()
    assert "Nieprawidłowa odpowiedź" in invalid_state_html
    assert "Nie udało się potwierdzić logowania" in invalid_state_html
    assert "Stan logowania" not in invalid_state_html
    assert "Nie udało się rozpocząć" in await bad_origin.text()
    assert "Sprawdź Discorda" in await missing_cookie.text()


@pytest.mark.parametrize("origin", ["null", "https://evil.example"])
async def test_rejected_origin_does_not_consume_verification_link(
    rso_test_client: TestClient, origin: str
) -> None:
    repository = rso_test_client.server.app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"

    rejected = await rso_test_client.post(
        path,
        headers={"Host": "localhost:8080", "Origin": origin},
        allow_redirects=False,
    )
    assert "Nie udało się rozpocząć" in await rejected.text()
    untouched = await repository.get_by_start_token(created.token)
    assert untouched is not None
    assert untouched.status == VerificationSessionStatus.CREATED.value

    started = await rso_test_client.post(
        path,
        headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"},
        allow_redirects=False,
    )
    assert started.status == 303
    assert urlsplit(started.headers["Location"]).netloc == "auth.riotgames.com"


@pytest.mark.parametrize("foreign_cookie", [None, "x" * 43])
async def test_callback_rejects_foreign_browser_before_contacting_riot(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, foreign_cookie: str | None
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    started = await rso_test_client.post(
        f"/verify/start/{created.token}",
        headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"},
        allow_redirects=False,
    )
    cookie = started.cookies[BROWSER_COOKIE]
    assert cookie["path"] == "/" and cookie["httponly"] and cookie["samesite"] == "Lax"
    state = parse_qs(urlsplit(started.headers["Location"]).query)["state"][0]
    exchange = AsyncMock(return_value="temporary-token")
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", exchange)
    headers = {"Host": "localhost:8080"}
    if foreign_cookie is not None:
        headers["Cookie"] = f"{BROWSER_COOKIE}={foreign_cookie}"
    rso_test_client.session.cookie_jar.clear()
    rejected = await rso_test_client.get(
        f"/oauth2/callback?state={state}&code=valid-code",
        headers=headers,
        allow_redirects=False,
    )
    assert "Wróć do poprzedniej przeglądarki" in await rejected.text()
    exchange.assert_not_awaited()
    record = await repository.get_by_start_token(created.token)
    assert record is not None and record.status == "AWAITING_RIOT"


async def test_superseded_callback_returns_polish_page_instead_of_500(
    rso_test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    started = await rso_test_client.post(
        f"/verify/start/{created.token}",
        headers={"Host": "localhost:8080"},
        allow_redirects=False,
    )
    state = parse_qs(urlsplit(started.headers["Location"]).query)["state"][0]

    async def supersede(_access_token: str) -> RiotIdentity:
        await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
        return RiotIdentity("not-linked", "Moon", "EUNE", "EUN1")

    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(return_value="token"))
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "get_identity", supersede)
    response = await rso_test_client.get(
        f"/oauth2/callback?state={state}&code=code",
        headers={
            "Host": "localhost:8080",
            "Cookie": f"{BROWSER_COOKIE}={started.cookies[BROWSER_COOKIE].value}",
        },
        allow_redirects=False,
    )
    assert response.status == 200
    assert "Ta weryfikacja nie jest już aktywna" in await response.text()


async def test_riot_json_waits_for_all_network_chunks() -> None:
    loop = asyncio.get_running_loop()
    protocol = Mock(spec=BaseProtocol, connected=True, _reading_paused=False)
    reader = StreamReader(protocol, limit=MAX_RIOT_RESPONSE_BYTES, loop=loop)
    reader.feed_data(b'{"access_')

    async def finish_response() -> None:
        await asyncio.sleep(0.01)
        reader.feed_data(b'token":"valid","token_type":"Bearer"}')
        reader.feed_eof()

    producer = asyncio.create_task(finish_response())
    try:
        payload = await _read_riot_json(
            cast(ClientResponse, SimpleNamespace(status=200, content=reader)),
            expected_status=200,
            endpoint="token",
        )
        assert payload == {"access_token": "valid", "token_type": "Bearer"}
    finally:
        await producer


@pytest.mark.parametrize(
    "body",
    [b"{}" + b" " * MAX_RIOT_RESPONSE_BYTES, b"{broken", b"[]"],
    ids=["too-large", "broken-json", "not-an-object"],
)
async def test_riot_json_rejects_oversized_or_invalid_payload(body: bytes) -> None:
    loop = asyncio.get_running_loop()
    protocol = Mock(spec=BaseProtocol, connected=True, _reading_paused=False)
    reader = StreamReader(protocol, limit=MAX_RIOT_RESPONSE_BYTES, loop=loop)
    reader.feed_data(body)
    reader.feed_eof()
    with pytest.raises(RiotRSOError):
        await _read_riot_json(
            cast(ClientResponse, SimpleNamespace(status=200, content=reader)),
            expected_status=200,
            endpoint="token",
        )


HOST = {"Host": "localhost:8080"}
ORIGIN = {**HOST, "Origin": "http://localhost:8080"}


def _state(response) -> str:
    return parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]


def _browser(client: TestClient) -> str:
    cookies = {cookie.key: cookie.value for cookie in client.session.cookie_jar}
    return cookies[BROWSER_COOKIE]


def test_new_link_hint_and_copy_avoid_dashes() -> None:
    assert NEW_LINK_HINT.endswith("a bot wyśle nowy link.")
    source = Path(__file__).parents[1].joinpath("moon_poro", "rso_web.py").read_text("utf-8")
    assert chr(0x2014) not in source and chr(0x2013) not in source


async def test_start_page_creates_browser_identity_once(rso_test_client: TestClient) -> None:
    repository = rso_test_client.server.app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"

    first = await rso_test_client.get(path, headers=HOST)
    cookie = first.cookies[BROWSER_COOKIE]
    assert cookie["path"] == "/" and cookie["httponly"] and cookie["samesite"] == "Lax"
    assert first.cookies["moon_poro_rso"]["path"] == "/verify"
    second = await rso_test_client.get(path, headers=HOST)
    assert BROWSER_COOKIE not in second.cookies

    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    assert started.status == 303
    assert started.cookies[BROWSER_COOKIE].value == cookie.value
    record = await repository.get_by_start_token(created.token)
    assert record is not None and record.status == "AWAITING_RIOT"


async def test_repeated_start_in_bound_browser_rotates_state(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    await rso_test_client.get(path, headers=HOST)

    first = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    second = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    assert first.status == second.status == 303
    assert urlsplit(second.headers["Location"]).netloc == "auth.riotgames.com"
    assert _state(first) != _state(second)

    # Back from Riot shows the start page again instead of a dead end.
    back = await rso_test_client.get(path, headers=HOST, allow_redirects=False)
    assert back.status == 200
    assert "Przejdź do logowania Riot" in await back.text()
    third = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    assert third.status == 303

    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(return_value="token"))
    monkeypatch.setattr(
        app[RSO_CLIENT_KEY],
        "get_identity",
        AsyncMock(return_value=RiotIdentity("rotated-puuid", "Moon", "EUNE", "EUN1")),
    )
    for stale in (first, second):
        rejected = await rso_test_client.get(
            f"/oauth2/callback?state={_state(stale)}&code=code", headers=HOST
        )
        assert "Ten link logowania jest nieaktualny" in await rejected.text()
    record = await repository.get_by_start_token(created.token)
    assert record is not None and record.status == "AWAITING_RIOT"
    accepted = await rso_test_client.get(
        f"/oauth2/callback?state={_state(third)}&code=code",
        headers=HOST,
        allow_redirects=False,
    )
    assert accepted.status == 303 and accepted.headers["Location"] == "/verify/result"
    record = await repository.get_by_start_token(created.token)
    assert record is not None and record.status == "VERIFIED_PENDING_DISCORD"


async def test_repeated_start_from_another_browser_is_rejected(
    rso_test_client: TestClient,
) -> None:
    repository = rso_test_client.server.app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    bound = await repository.get_by_start_token(created.token)
    assert bound is not None

    for cookie in (None, "y" * 43):
        rso_test_client.session.cookie_jar.clear()
        headers = dict(ORIGIN)
        if cookie is not None:
            headers["Cookie"] = f"{BROWSER_COOKIE}={cookie}"
        rejected = await rso_test_client.post(path, headers=headers, allow_redirects=False)
        assert rejected.status == 200
        assert "Logowanie trwa w innej przeglądarce" in await rejected.text()
        assert BROWSER_COOKIE not in rejected.cookies
        foreign_get = await rso_test_client.get(path, headers=headers, allow_redirects=False)
        assert foreign_get.status == 303 and foreign_get.headers["Location"] == "/verify/result"

    after = await repository.get_by_start_token(created.token)
    assert after is not None and after.status == "AWAITING_RIOT"
    assert after.oauth_state_hash == bound.oauth_state_hash
    assert after.browser_binding_hash == bound.browser_binding_hash
    still_valid = await repository.claim_callback(
        _state(started), browser_token=started.cookies[BROWSER_COOKIE].value
    )
    assert still_valid.status == "PROCESSING_RIOT"


async def test_callback_reload_while_processing_redirects_to_result(
    rso_test_client: TestClient,
) -> None:
    repository = rso_test_client.server.app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    await rso_test_client.get(path, headers=HOST)
    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    browser = _browser(rso_test_client)
    await repository.claim_callback(_state(started), browser_token=browser)

    reloaded = await rso_test_client.get(
        f"/oauth2/callback?state={_state(started)}&code=code",
        headers=HOST,
        allow_redirects=False,
    )
    assert reloaded.status == 303 and reloaded.headers["Location"] == "/verify/result"
    page = await rso_test_client.get("/verify/result", headers=HOST)
    assert "Potwierdzamy logowanie w Riot" in await page.text()

    rso_test_client.session.cookie_jar.clear()
    foreign = await rso_test_client.get(
        f"/oauth2/callback?state={_state(started)}&code=code",
        headers={**HOST, "Cookie": f"{BROWSER_COOKIE}={'z' * 43}"},
        allow_redirects=False,
    )
    assert "Wróć do poprzedniej przeglądarki" in await foreign.text()


@pytest.mark.parametrize("fail_also_breaks", [False, True])
async def test_unexpected_error_after_claim_fails_session_instead_of_polling(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch, fail_also_breaks: bool
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    await rso_test_client.get(path, headers=HOST)
    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(return_value="token"))
    monkeypatch.setattr(
        app[RSO_CLIENT_KEY],
        "get_identity",
        AsyncMock(return_value=RiotIdentity("broken-puuid", "Moon", "EUNE", "EUN1")),
    )
    monkeypatch.setattr(
        repository, "reserve_link", AsyncMock(side_effect=RuntimeError("database down"))
    )
    if fail_also_breaks:
        monkeypatch.setattr(repository, "fail", AsyncMock(side_effect=RuntimeError("still down")))

    response = await rso_test_client.get(
        f"/oauth2/callback?state={_state(started)}&code=code",
        headers=HOST,
        allow_redirects=False,
    )

    assert response.status == 303 and response.headers["Location"] == "/verify/result"
    record = await repository.get_by_start_token(created.token)
    assert record is not None
    if fail_also_breaks:
        assert record.status == "PROCESSING_RIOT"
        return
    assert (record.status, record.error_code) == ("FAILED", "INTERNAL_ERROR")
    page = await rso_test_client.get("/verify/result", headers=HOST)
    assert "Nie udało się połączyć konta" in await page.text()
    assert "Refresh" not in page.headers


async def test_internal_error_never_fails_a_session_that_reserved_its_link(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    await rso_test_client.get(path, headers=HOST)
    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(return_value="token"))
    monkeypatch.setattr(
        app[RSO_CLIENT_KEY],
        "get_identity",
        AsyncMock(return_value=RiotIdentity("ambiguous-puuid", "Moon", "EUNE", "EUN1")),
    )
    reserve = repository.reserve_link

    async def reserve_then_lose_connection(**kwargs):
        await reserve(**kwargs)
        raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(repository, "reserve_link", reserve_then_lose_connection)

    response = await rso_test_client.get(
        f"/oauth2/callback?state={_state(started)}&code=code",
        headers=HOST,
        allow_redirects=False,
    )

    assert response.headers["Location"] == "/verify/result"
    record = await repository.get_by_start_token(created.token)
    assert record is not None and record.status == "VERIFIED_PENDING_DISCORD"


async def test_cancelled_callback_still_marks_session_failed(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    await rso_test_client.get(path, headers=HOST)
    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    monkeypatch.setattr(
        app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(aiohttp.ClientError):
        await rso_test_client.get(
            f"/oauth2/callback?state={_state(started)}&code=code",
            headers=HOST,
            allow_redirects=False,
        )
    # The handler schedules the update before re-raising the cancellation.
    pending = set(_BACKGROUND_TASKS)
    assert pending
    await asyncio.wait(pending, timeout=5)
    record = await repository.get_by_start_token(created.token)
    assert record is not None
    assert (record.status, record.error_code) == ("FAILED", "INTERNAL_ERROR")


async def test_reservation_after_expiry_reports_expired_session(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = rso_test_client.server.app
    repository = app[REPOSITORY_KEY]
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    path = f"/verify/start/{created.token}"
    await rso_test_client.get(path, headers=HOST)
    started = await rso_test_client.post(path, headers=ORIGIN, allow_redirects=False)
    monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(return_value="token"))
    monkeypatch.setattr(
        app[RSO_CLIENT_KEY],
        "get_identity",
        AsyncMock(return_value=RiotIdentity("late-puuid", "Moon", "EUNE", "EUN1")),
    )
    monkeypatch.setattr(repository, "reserve_link", AsyncMock(side_effect=SessionExpired))

    response = await rso_test_client.get(
        f"/oauth2/callback?state={_state(started)}&code=code",
        headers=HOST,
        allow_redirects=False,
    )

    assert response.headers["Location"] == "/verify/result"
    record = await repository.get_by_start_token(created.token)
    assert record is not None
    assert (record.status, record.error_code) == ("FAILED", "SESSION_EXPIRED")
    page = await rso_test_client.get("/verify/result", headers=HOST)
    assert "Sesja wygasła" in await page.text() and "Refresh" not in page.headers


@pytest.mark.parametrize("status", ["CREATED", "AWAITING_RIOT", "PROCESSING_RIOT"])
def test_unfinished_session_past_expiry_stops_polling(status: str) -> None:
    record = result_record(status, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    response = _result_for_record(record)

    assert "Sesja wygasła" in response.text
    assert "Refresh" not in response.headers


def test_awaiting_riot_result_polls_slowly_without_reporting_failure() -> None:
    response = _result_for_record(result_record("AWAITING_RIOT"))

    assert "Logowanie w Riot jeszcze trwa" in response.text
    assert "Nie udało się" not in response.text
    assert response.headers["Refresh"] == "10"


@pytest.mark.parametrize("status", ["VERIFIED_PENDING_DISCORD", "APPLYING_DISCORD"])
def test_slow_discord_completion_is_calm_and_never_expires(status: str) -> None:
    long_ago = datetime.now(UTC) - timedelta(minutes=30)
    record = result_record(
        status,
        expires_at=long_ago,
        updated_at=datetime.now(UTC),
        verification_link_created_at=long_ago,
    )

    response = _result_for_record(record)

    assert "Riot potwierdził konto" in response.text
    assert "role pojawią się same" in response.text
    assert "wygasła" not in response.text and "Nie udało się" not in response.text
    assert response.headers["Refresh"] == "30"


def test_superseded_session_explains_newer_link() -> None:
    response = _result_for_record(result_record("CANCELLED", error_code="SUPERSEDED"))

    assert "Masz już nowszy link" in response.text
    assert "Logowanie anulowane" not in response.text


async def test_unhandled_error_renders_polish_500_with_security_headers(
    rso_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = rso_test_client.server.app[REPOSITORY_KEY]
    monkeypatch.setattr(
        repository, "get_by_start_token", AsyncMock(side_effect=RuntimeError("database down"))
    )

    response = await rso_test_client.get(
        "/verify/result", headers={**HOST, "Cookie": f"moon_poro_rso={'a' * 43}"}
    )

    assert response.status == 500
    assert "Coś poszło nie tak" in await response.text()
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_readiness_is_hidden_from_proxied_requests(rso_test_client: TestClient) -> None:
    for header in ("X-Forwarded-For", "Forwarded"):
        response = await rso_test_client.get("/readyz", headers={**HOST, header: "203.0.113.9"})
        assert response.status == 404
        assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert (await rso_test_client.get("/readyz", headers=HOST)).status == 200


async def test_riot_client_pool_and_timeouts(rso_test_client: TestClient) -> None:
    session = rso_test_client.server.app[HTTP_CLIENT_KEY]
    connector = session.connector
    assert connector is not None
    assert connector.limit == 16 and connector.limit_per_host == 8
    timeout = session.timeout
    assert timeout.sock_connect == 10 and timeout.sock_read == 10
    assert timeout.connect is None and timeout.total == 20


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            b'{"error":"invalid_grant",'
            b'"error_description":"code abcdefghijklmnopqrstuvwxyz0123 expired"}',
            "error=invalid_grant, error_description=code [redacted] expired",
        ),
        (b"<html>Bad gateway</html>", "error=-, error_description=-"),
    ],
    ids=["oauth-json", "not-json"],
)
async def test_riot_rejection_logs_endpoint_and_oauth_error_without_secrets(
    caplog: pytest.LogCaptureFixture, body: bytes, expected: str
) -> None:
    loop = asyncio.get_running_loop()
    protocol = Mock(spec=BaseProtocol, connected=True, _reading_paused=False)
    reader = StreamReader(protocol, limit=MAX_RIOT_RESPONSE_BYTES, loop=loop)
    reader.feed_data(body)
    reader.feed_eof()

    with caplog.at_level(logging.WARNING, logger="moon_poro.rso"):
        with pytest.raises(RiotRSOError, match="RIOT_UPSTREAM_REJECTED"):
            await _read_riot_json(
                cast(ClientResponse, SimpleNamespace(status=400, content=reader)),
                expected_status=200,
                endpoint="userinfo",
            )

    assert "RSO userinfo endpoint returned HTTP 400" in caplog.text
    assert expected in caplog.text
    assert "abcdefghijklmnopqrstuvwxyz0123" not in caplog.text
