"""End-to-end checks of the bot's Riot client against a fake Riot gateway."""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pulsefire.invocation import Invocation
from pulsefire.middlewares import rate_limiter_middleware
from pulsefire.ratelimiters import RiotAPIRateLimiter

from moon_poro import riot
from moon_poro.riot import (
    RiotAPIInvalidResponse,
    RiotAPIUnavailable,
    RiotAuthBreaker,
    RiotAuthCircuitOpen,
    riot_api_call,
    riot_rate_limiter_middleware,
)

RATE_LIMIT_HEADERS = {
    "X-App-Rate-Limit": "20:1,100:120",
    "X-App-Rate-Limit-Count": "1:1,1:120",
    "X-Method-Rate-Limit": "2000:10",
    "X-Method-Rate-Limit-Count": "1:10",
}
FORBIDDEN = {"status": {"message": "Forbidden", "status_code": 403}}


@pytest.fixture(autouse=True)
def fresh_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pulsefire keeps limiter state on the class; isolate every test.
    monkeypatch.setattr(RiotAPIRateLimiter, "_index", defaultdict(lambda: (0, 0, 0, 0, 0)))


class FakeRiotGateway:
    """Route like Riot's gateway: unknown paths get 403, not 404."""

    def __init__(self) -> None:
        self.raw_paths: list[str] = []
        self.league_body: Callable[[], web.Response] = lambda: web.json_response([])
        self.statuses: list[int] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.raw_paths.append(request.raw_path)
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return web.json_response({}, status=status, headers={"Retry-After": "0"})
        segments = [unquote(part) for part in request.raw_path.split("?")[0].split("/")[1:]]
        match segments:
            case ["riot", "account", "v1", "accounts", "by-riot-id", name, tag]:
                if name == "forbidden":
                    return web.json_response(FORBIDDEN, status=403)
                if name == "Secret Name":
                    return web.json_response({}, status=400)
                return web.json_response({"puuid": "P", "gameName": name, "tagLine": tag})
            case ["riot", "account", "v1", "accounts", "by-puuid", puuid]:
                return web.json_response({"puuid": puuid, "gameName": "x", "tagLine": "EUW"})
            case ["lol", "summoner", "v4", "summoners", "by-puuid", puuid]:
                if puuid == "forbidden":
                    return web.json_response(FORBIDDEN, status=403)
                return web.json_response({"puuid": puuid}, headers=RATE_LIMIT_HEADERS)
            case ["lol", "league", "v4", "entries", "by-puuid", _puuid]:
                return self.league_body()
        return web.json_response(FORBIDDEN, status=403)


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[FakeRiotGateway, str]]:
    fake = FakeRiotGateway()
    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, f"http://{server.host}:{server.port}"
    finally:
        await server.close()


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def sleep(delay: float, *args: Any) -> None:
        sleeps.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("pulsefire.middlewares.asyncio.sleep", sleep)
    return sleeps


def by_riot_id(client: Any, name: str) -> Callable[[], Any]:
    return lambda: client.get_account_v1_by_riot_id(region="europe", game_name=name, tag_line="EUW")


@pytest.mark.parametrize(
    "name",
    ["ab/cd", "a?b", "a#b", "a%2Fb", "100%", "a b", "a\\b", "x\ty", "Zażółć", "a/../b"],
)
async def test_riot_id_path_characters_stay_inside_one_segment(
    gateway: tuple[FakeRiotGateway, str], name: str
) -> None:
    fake, base_url = gateway
    breaker = RiotAuthBreaker()
    async with riot.create_riot_api_client("test-only", auth_breaker=breaker) as client:
        client.base_url = base_url
        account = await riot_api_call(by_riot_id(client, name), not_found=None)

    assert account == {"puuid": "P", "gameName": name, "tagLine": "EUW"}
    assert len(fake.raw_paths) == 1
    assert not breaker.snapshot(scope="/riot/account/v1").blocked


async def test_slash_in_riot_id_does_not_block_account_api_for_everyone(
    gateway: tuple[FakeRiotGateway, str],
) -> None:
    fake, base_url = gateway
    breaker = RiotAuthBreaker(probe_interval_seconds=900)
    async with riot.create_riot_api_client("test-only", auth_breaker=breaker) as client:
        client.base_url = base_url
        attacker = await riot_api_call(by_riot_id(client, "ab/cd"), not_found=None)
        legit = await riot_api_call(by_riot_id(client, "Good Name"), not_found=None)
        by_puuid = await riot_api_call(
            lambda: client.get_account_v1_by_puuid(region="europe", puuid="abc")
        )

    assert attacker is not None and attacker["gameName"] == "ab/cd"
    assert legit is not None and legit["gameName"] == "Good Name"
    assert by_puuid["puuid"] == "abc"
    assert fake.raw_paths[0] == "/riot/account/v1/accounts/by-riot-id/ab%2Fcd/EUW"
    assert breaker.can_attempt(scope="/riot/account/v1")


@pytest.mark.parametrize("name", ["", ".", ".."])
async def test_dot_segments_never_reach_riot(
    gateway: tuple[FakeRiotGateway, str], name: str
) -> None:
    fake, base_url = gateway
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        assert await riot_api_call(by_riot_id(client, name), not_found=None) is None

    assert fake.raw_paths == []


async def test_endpoint_gate_needs_consecutive_403s(gateway: tuple[FakeRiotGateway, str]) -> None:
    fake, base_url = gateway
    breaker = RiotAuthBreaker(probe_interval_seconds=900)
    scope = "/riot/account/v1"

    async def forbidden() -> None:
        with pytest.raises(RiotAPIUnavailable) as raised:
            await riot_api_call(by_riot_id(client, "forbidden"), not_found=None)
        assert raised.value.status == 403

    async with riot.create_riot_api_client("test-only", auth_breaker=breaker) as client:
        client.base_url = base_url
        await forbidden()
        await forbidden()
        # Any other response resets the streak.
        assert await riot_api_call(by_riot_id(client, "Good Name"), not_found=None)
        await forbidden()
        await forbidden()
        assert breaker.can_attempt(scope=scope)
        await forbidden()
        assert not breaker.can_attempt(scope=scope)
        sent = len(fake.raw_paths)
        with pytest.raises(RiotAuthCircuitOpen):
            await riot_api_call(by_riot_id(client, "Good Name"), not_found=None)
        assert len(fake.raw_paths) == sent
        # Other API families and the shared credential stay usable.
        assert breaker.can_attempt(scope=riot.RIOT_LEAGUE_AUTH_SCOPE)
        assert not breaker.snapshot().blocked


async def test_short_circuited_request_does_not_stall_the_next_one(
    gateway: tuple[FakeRiotGateway, str],
) -> None:
    fake, base_url = gateway
    breaker = RiotAuthBreaker()
    async with riot.create_riot_api_client("test-only", auth_breaker=breaker) as client:
        client.base_url = base_url
        for _ in range(riot.RIOT_FORBIDDEN_THRESHOLD):
            with pytest.raises(RiotAPIUnavailable):
                await riot_api_call(
                    lambda: client.get_lol_summoner_v4_by_puuid(region="eun1", puuid="forbidden")
                )
        with pytest.raises(RiotAuthCircuitOpen):
            await riot_api_call(lambda: client.get_lol_summoner_v4_by_id(region="eun1", id="x"))

        started = time.monotonic()
        leagues = await asyncio.wait_for(
            riot_api_call(
                lambda: client.get_lol_league_v4_entries_by_puuid(region="eun1", puuid="p")
            ),
            timeout=5,
        )

    assert leagues == []
    assert time.monotonic() - started < 1
    assert len(fake.raw_paths) == riot.RIOT_FORBIDDEN_THRESHOLD + 1


@pytest.mark.parametrize("error", [TimeoutError(), RiotAuthCircuitOpen(status=403)])
async def test_rate_limiter_lease_is_released_when_no_response_arrives(error: Exception) -> None:
    def invocation(path: str) -> Invocation:
        return Invocation("GET", "https://{region}.api.riotgames.com" + path, {"region": "eun1"})

    limiter = RiotAPIRateLimiter()
    failing = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        await rate_limiter_middleware(limiter)(failing)(invocation("/a"))
    # Pulsefire alone leaves the region's app limits "pinging": others poll.
    assert await limiter.acquire(invocation("/b")) == 0.1

    RiotAPIRateLimiter._index.clear()
    with pytest.raises(type(error)):
        await riot_rate_limiter_middleware(limiter)(failing)(invocation("/a"))
    assert await limiter.acquire(invocation("/b")) == -1


@pytest.mark.parametrize(
    "body",
    [
        lambda: web.Response(body=b""),
        lambda: web.Response(text="<html>Bad gateway</html>"),
        lambda: web.json_response({"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}),
        lambda: web.json_response(["RANKED_SOLO_5x5"]),
        lambda: web.json_response([{"queueType": "RANKED_SOLO_5x5"}]),
        lambda: web.json_response([{"queueType": "RANKED_SOLO_5x5", "tier": "UNRANKED"}]),
    ],
)
async def test_invalid_league_payload_is_retryable_not_unranked(
    gateway: tuple[FakeRiotGateway, str], body: Callable[[], web.Response]
) -> None:
    fake, base_url = gateway
    fake.league_body = body
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        with pytest.raises(RiotAPIInvalidResponse):
            await riot_api_call(
                lambda: client.get_lol_league_v4_entries_by_puuid(region="eun1", puuid="p")
            )


async def test_valid_league_payload_is_returned(gateway: tuple[FakeRiotGateway, str]) -> None:
    fake, base_url = gateway
    entries = [
        {"queueType": "RANKED_FLEX_SR"},
        {"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD", "rank": "II"},
    ]
    fake.league_body = lambda: web.json_response(entries)
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        result = await riot_api_call(
            lambda: client.get_lol_league_v4_entries_by_puuid(region="eun1", puuid="p")
        )

    assert result == entries


async def test_failed_request_logs_endpoint_template_without_riot_id(
    gateway: tuple[FakeRiotGateway, str], caplog: pytest.LogCaptureFixture
) -> None:
    _fake, base_url = gateway
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        with caplog.at_level(logging.DEBUG), pytest.raises(RiotAPIUnavailable) as raised:
            await riot_api_call(by_riot_id(client, "Secret Name"), not_found=None)

    assert raised.value.status == 400
    assert raised.value.__cause__ is None
    records = [record for record in caplog.records if record.name == "moon_poro.riot"]
    assert records
    for record in records:
        assert record.exc_info is None
        assert "Secret" not in record.getMessage()
    assert "/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}" in records[-1].getMessage()
    assert not any(
        "Secret" in record.getMessage()
        for record in caplog.records
        if record.name != "aiohttp.access"  # The fake gateway's own access log.
    )


async def test_path_params_are_encoded_once_on_a_copy() -> None:
    next_call = AsyncMock(return_value="ok")
    original = Invocation(
        "GET",
        "https://{region}.api.riotgames.com/lol/champion-mastery/v4/champion-masteries"
        "/by-summoner/{summoner_id}/by-champion/{champion_id}",
        {"region": "eun1", "summoner_id": "x\ny/%", "champion_id": 17, "queries": {"a": "b/c"}},
    )

    assert await riot.riot_path_params_middleware()(next_call)(original) == "ok"

    sent = next_call.await_args.args[0]
    assert sent.url == (
        "https://eun1.api.riotgames.com/lol/champion-mastery/v4/champion-masteries"
        "/by-summoner/x%0Ay%2F%25/by-champion/17?a=b%2Fc"
    )
    assert sent.uid == original.uid
    assert original.params["summoner_id"] == "x\ny/%"


async def test_server_errors_are_retried_once(
    gateway: tuple[FakeRiotGateway, str], fast_backoff: list[float]
) -> None:
    fake, base_url = gateway
    fake.statuses = [503, 503, 503, 503]
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        with pytest.raises(RiotAPIUnavailable) as raised:
            await riot_api_call(by_riot_id(client, "Good Name"), not_found=None)

    assert raised.value.status == 503
    assert len(fake.raw_paths) == 2
    assert [delay for delay in fast_backoff if delay >= 1] == [2]


async def test_rate_limited_request_is_retried_after_retry_after(
    gateway: tuple[FakeRiotGateway, str], fast_backoff: list[float]
) -> None:
    fake, base_url = gateway
    fake.statuses = [429]
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        account = await riot_api_call(by_riot_id(client, "Good Name"), not_found=None)

    assert account is not None and account["gameName"] == "Good Name"
    assert len(fake.raw_paths) == 2


async def test_rate_limited_twice_reports_retry_after(
    gateway: tuple[FakeRiotGateway, str], fast_backoff: list[float]
) -> None:
    fake, base_url = gateway
    fake.statuses = [429, 429]
    async with riot.create_riot_api_client("test-only") as client:
        client.base_url = base_url
        with pytest.raises(RiotAPIUnavailable) as raised:
            await riot_api_call(by_riot_id(client, "Good Name"), not_found=None)

    assert raised.value.status == 429
    assert raised.value.retry_after == 0


@pytest.mark.parametrize(("error", "traceback"), [(TimeoutError(), False), (KeyError("x"), True)])
async def test_only_unexpected_errors_keep_a_traceback(
    error: Exception, traceback: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="moon_poro.riot"):
        with pytest.raises(RiotAPIUnavailable):
            await riot_api_call(AsyncMock(side_effect=error))

    (record,) = caplog.records
    assert (record.exc_info is not None) is traceback
