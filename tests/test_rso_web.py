from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from moon_poro.database import Database
from moon_poro.models import Base, VerificationSessionStatus
from moon_poro.rso_web import (
    REPOSITORY_KEY,
    RSO_CLIENT_KEY,
    RequestRateLimiter,
    RiotIdentity,
    RiotRSOClient,
    RiotRSOError,
    _result_for_record,
    create_app,
)
from moon_poro.settings import RSOSettings


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


def test_completed_result_escapes_riot_id() -> None:
    record = SimpleNamespace(
        status=VerificationSessionStatus.COMPLETED.value,
        riot_game_name="<script>",
        riot_tag_line="tag&line",
        guild_id=123,
        error_code=None,
    )

    response = _result_for_record(record)

    assert "<script>" not in response.text
    assert "&lt;script&gt;#tag&amp;line" in response.text
    assert "https://discord.com/channels/123" in response.text


def test_rate_limiter_rejects_request_after_limit() -> None:
    limiter = RequestRateLimiter(limit=2, window_seconds=60)

    assert limiter.allowed("127.0.0.1")
    assert limiter.allowed("127.0.0.1")
    assert not limiter.allowed("127.0.0.1")


async def test_health_readiness_and_security_headers(rso_test_client: TestClient) -> None:
    headers = {"Host": "localhost:8080"}

    health = await rso_test_client.get("/healthz", headers=headers)
    ready = await rso_test_client.get("/readyz", headers=headers)
    invalid_host = await rso_test_client.get("/healthz", headers={"Host": "evil.example"})

    assert await health.json() == {"status": "ok"}
    assert await ready.json() == {"status": "ready"}
    assert health.headers["X-Frame-Options"] == "DENY"
    assert health.headers["Cache-Control"] == "no-store, max-age=0"
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
    assert await responses[-1].text() == "Za dużo żądań. Spróbuj ponownie za minutę."


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
    assert "Potwierdź dostęp do Riot ID i regionu" in landing_html
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
        headers=headers,
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
    assert "Gotowe — konto zostało zweryfikowane" in completed_html
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
        headers={"Host": "localhost:8080"},
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
