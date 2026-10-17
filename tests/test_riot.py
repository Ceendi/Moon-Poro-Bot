import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestServer
from pulsefire.invocation import Invocation
from pulsefire.ratelimiters import BaseRateLimiter

from moon_poro import riot
from moon_poro.riot import (
    RiotAPIInvalidResponse,
    RiotAPIMonitor,
    RiotAPINotFound,
    RiotAPIUnavailable,
    RiotAuthBreaker,
    RiotAuthCircuitOpen,
    RiotCircuitBreaker,
    get_rank_from_leagues,
    profile_icon_url,
    riot_api_call,
    riot_api_monitoring_middleware,
    riot_auth_breaker_middleware,
    riot_circuit_breaker_middleware,
)


class RiotResponseError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class RiotResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


def test_solo_rank_is_selected() -> None:
    leagues = [
        {"queueType": "RANKED_FLEX_SR", "tier": "DIAMOND"},
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"},
    ]
    assert get_rank_from_leagues(leagues) == "GOLD"


def test_missing_solo_rank_is_unranked() -> None:
    assert get_rank_from_leagues([]) == "UNRANKED"


@pytest.mark.parametrize(
    "leagues",
    [
        [{"queueType": "RANKED_SOLO_5x5"}],
        [{"queueType": "RANKED_SOLO_5x5", "tier": 3}],
        [{"queueType": "RANKED_SOLO_5x5", "tier": "WOOD"}],
        b"",
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"},
        ["GOLD"],
    ],
)
def test_malformed_leagues_are_an_error_not_unranked(leagues: object) -> None:
    with pytest.raises(RiotAPIInvalidResponse):
        get_rank_from_leagues(leagues)  # type: ignore[arg-type]


def test_cached_unranked_and_lowercase_tiers_are_accepted() -> None:
    assert get_rank_from_leagues([{"queueType": "RANKED_SOLO_5x5", "tier": "UNRANKED"}]) == (
        "UNRANKED"
    )
    assert get_rank_from_leagues([{"queueType": "RANKED_SOLO_5x5", "tier": "gold"}]) == "GOLD"


def test_profile_icon_url_uses_requested_icon() -> None:
    assert profile_icon_url(17).endswith("/17.jpg")


async def test_circuit_breaker_pauses_all_calls_for_the_limited_region() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    breaker = RiotCircuitBreaker(clock=lambda: now[0], sleep=sleep)
    next_call = AsyncMock(
        side_effect=[
            RiotResponse(429, {"Retry-After": "7"}),
            RiotResponse(200),
            RiotResponse(200),
        ]
    )
    middleware = riot_circuit_breaker_middleware(breaker)(next_call)
    eun1 = Invocation("GET", "https://{region}.api.riotgames.com", {"region": "EUN1"})
    euw1 = Invocation("GET", "https://{region}.api.riotgames.com", {"region": "EUW1"})

    await middleware(eun1)
    await middleware(euw1)
    await middleware(eun1)

    assert sleeps == [7.0]
    assert next_call.await_count == 3


async def test_circuit_breaker_halts_pulsefire_retry_until_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    pulsefire_sleeps: list[float] = []
    breaker_sleeps: list[float] = []

    async def pulsefire_sleep(delay: float) -> None:
        pulsefire_sleeps.append(delay)
        now[0] += delay

    async def breaker_sleep(delay: float) -> None:
        breaker_sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr("pulsefire.middlewares.asyncio.sleep", pulsefire_sleep)
    breaker = RiotCircuitBreaker(clock=lambda: now[0], sleep=breaker_sleep)
    transport = AsyncMock(side_effect=[RiotResponse(429, {"Retry-After": "7"}), RiotResponse(200)])
    middleware = riot.http_error_middleware(max_retries=1)(
        riot_circuit_breaker_middleware(breaker)(transport)
    )
    invocation = Invocation("GET", "https://{region}.api.riotgames.com", {"region": "EUN1"})

    response = await middleware(invocation)

    assert response.status == 200
    assert pulsefire_sleeps == [2]
    assert breaker_sleeps == [5]
    assert transport.await_count == 2


async def test_circuit_breaker_honours_an_extended_retry_after() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    breaker = RiotCircuitBreaker(clock=lambda: now[0], sleep=sleep)
    breaker.block("eun1", 5)
    now[0] += 2
    breaker.block("EUN1", 10)

    await breaker.wait("eun1")

    assert sleeps == [10.0]
    assert breaker.retry_after("eun1") == 0


async def test_circuit_breaker_uses_safe_fallback_for_invalid_retry_after() -> None:
    now = [100.0]
    breaker = RiotCircuitBreaker(clock=lambda: now[0])
    next_call = AsyncMock(return_value=RiotResponse(429, {"Retry-After": "invalid"}))
    middleware = riot_circuit_breaker_middleware(breaker)(next_call)
    invocation = Invocation("GET", "https://{region}.api.riotgames.com", {"region": "NA1"})

    await middleware(invocation)

    assert breaker.retry_after("na1") == 1.0


async def test_riot_monitor_counts_selected_raw_responses_and_last_success() -> None:
    successful_at = datetime(2026, 8, 14, 20, 30, tzinfo=UTC)
    monitor = RiotAPIMonitor(clock=lambda: successful_at)
    next_call = AsyncMock(
        side_effect=[
            RiotResponse(429),
            RiotResponse(401),
            RiotResponse(403),
            RiotResponse(500),
            RiotResponse(503),
            RiotResponse(404),
            RiotResponse(200),
        ]
    )
    middleware = riot_api_monitoring_middleware(monitor)(next_call)
    invocation = Invocation("GET", "https://{region}.api.riotgames.com", {"region": "EUN1"})

    for _ in range(7):
        await middleware(invocation)

    metrics = monitor.snapshot()
    assert metrics.responses_429 == 1
    assert metrics.responses_401 == 1
    assert metrics.responses_403 == 1
    assert metrics.responses_5xx == 2
    assert metrics.last_success_at == successful_at


def test_riot_client_places_monitor_and_shared_breaker_near_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_middleware = object()
    http_error = object()
    breaker_middleware = object()
    auth_middleware = object()
    monitoring_middleware = object()
    rate_limiter = object()
    league_payload = object()
    path_params = object()
    client = object()
    client_class = Mock(return_value=client)
    breaker_factory = Mock(return_value=breaker_middleware)
    auth_factory = Mock(return_value=auth_middleware)
    monitoring_factory = Mock(return_value=monitoring_middleware)
    http_error_factory = Mock(return_value=http_error)
    breaker = RiotCircuitBreaker()
    auth_breaker = RiotAuthBreaker()
    monitor = RiotAPIMonitor()
    monkeypatch.setattr(riot, "_BoundedRiotAPIClient", client_class)
    monkeypatch.setattr(riot, "json_response_middleware", Mock(return_value=json_middleware))
    monkeypatch.setattr(riot, "http_error_middleware", http_error_factory)
    monkeypatch.setattr(riot, "riot_circuit_breaker_middleware", breaker_factory)
    monkeypatch.setattr(riot, "riot_auth_breaker_middleware", auth_factory)
    monkeypatch.setattr(riot, "riot_api_monitoring_middleware", monitoring_factory)
    monkeypatch.setattr(riot, "riot_rate_limiter_middleware", Mock(return_value=rate_limiter))
    monkeypatch.setattr(riot, "riot_league_payload_middleware", Mock(return_value=league_payload))
    monkeypatch.setattr(riot, "riot_path_params_middleware", Mock(return_value=path_params))

    result = riot.create_riot_api_client(
        "test-token",
        circuit_breaker=breaker,
        auth_breaker=auth_breaker,
        monitor=monitor,
    )

    assert result is client
    breaker_factory.assert_called_once_with(breaker)
    auth_factory.assert_called_once_with(auth_breaker)
    monitoring_factory.assert_called_once_with(monitor)
    http_error_factory.assert_called_once_with(max_retries=1)
    assert client_class.call_args.kwargs["middlewares"] == [
        league_payload,
        path_params,
        json_middleware,
        http_error,
        rate_limiter,
        breaker_middleware,
        auth_middleware,
        monitoring_middleware,
    ]


async def test_requests_waiting_in_the_limiter_are_refused_after_a_401() -> None:
    class SlowLimiter(BaseRateLimiter):
        async def acquire(self, invocation: Invocation) -> float:
            if invocation.params["who"] == "first":
                return 0
            if invocation.params.setdefault("waited", False):
                return 0
            invocation.params["waited"] = True
            return 0.05

        async def synchronize(self, invocation: Invocation, headers: dict[str, str]) -> None:
            return None

    sent: list[str] = []

    async def transport(invocation: Invocation) -> SimpleNamespace:
        sent.append(invocation.params["who"])
        await asyncio.sleep(0.02)
        return SimpleNamespace(status=401, headers={})

    call = riot.riot_rate_limiter_middleware(SlowLimiter())(
        riot.riot_auth_breaker_middleware(RiotAuthBreaker())(transport)
    )

    async def request(who: str) -> object:
        invocation = Invocation(
            "GET",
            "https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}",
            {"region": "eun1", "puuid": "p", "who": who},
        )
        try:
            return (await call(invocation)).status
        except riot.RiotAuthCircuitOpen:
            return "refused"

    results = await asyncio.gather(*(request(who) for who in ("first", "a", "b", "c")))

    assert results == [401, "refused", "refused", "refused"]
    assert sent == ["first"]


async def test_riot_api_call_returns_successful_response() -> None:
    operation = AsyncMock(return_value={"puuid": "value"})

    result = await riot_api_call(operation)

    assert result == {"puuid": "value"}
    operation.assert_awaited_once_with()


async def test_riot_api_call_maps_not_found_to_fallback() -> None:
    operation = AsyncMock(side_effect=RiotResponseError(404))

    result: list[object] | None = await riot_api_call(operation, not_found=[])

    assert result == []
    operation.assert_awaited_once_with()


async def test_riot_api_call_keeps_unhandled_not_found_distinct_from_empty_200() -> None:
    operation = AsyncMock(side_effect=RiotResponseError(404))

    with pytest.raises(RiotAPINotFound):
        await riot_api_call(operation)


async def test_auth_breaker_allows_only_one_probe_and_recovers() -> None:
    now = [100.0]
    breaker = RiotAuthBreaker(probe_interval_seconds=900, clock=lambda: now[0])
    first = AsyncMock(return_value=RiotResponse(401))
    middleware = riot_auth_breaker_middleware(breaker)(first)
    invocation = Invocation("GET", "https://{region}.api.riotgames.com", {"region": "EUN1"})

    await middleware(invocation)
    assert breaker.snapshot().blocked
    with pytest.raises(RiotAuthCircuitOpen):
        await middleware(invocation)

    now[0] += 900
    probe_started = breaker.acquire()
    assert probe_started
    with pytest.raises(RiotAuthCircuitOpen):
        breaker.acquire()
    breaker.record_status(200, was_probe=probe_started)
    assert not breaker.snapshot().blocked


async def test_forbidden_api_family_does_not_pause_other_riot_apis() -> None:
    now = [100.0]
    breaker = RiotAuthBreaker(clock=lambda: now[0])
    transport = AsyncMock(
        side_effect=[*(RiotResponse(403) for _ in range(3)), RiotResponse(200), RiotResponse(200)]
    )
    middleware = riot_auth_breaker_middleware(breaker)(transport)
    summoner = Invocation(
        "GET",
        "https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}",
        {"region": "EUN1", "puuid": "test-puuid"},
    )
    league = Invocation(
        "GET",
        "https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}",
        {"region": "EUN1", "puuid": "test-puuid"},
    )
    for _ in range(2):
        await middleware(summoner)
        assert breaker.can_attempt(scope="/lol/summoner/v4")
    await middleware(summoner)
    assert breaker.can_attempt()
    assert not breaker.can_attempt(scope="/lol/summoner/v4")
    assert breaker.can_attempt(scope=riot.RIOT_LEAGUE_AUTH_SCOPE)
    assert breaker.snapshot(scope="/lol/summoner/v4").blocked
    assert not breaker.snapshot(scope=riot.RIOT_LEAGUE_AUTH_SCOPE).blocked
    with pytest.raises(RiotAuthCircuitOpen):
        await middleware(summoner)
    assert (await middleware(league)).status == 200
    now[0] += 900
    assert (await middleware(summoner)).status == 200
    assert not breaker.snapshot(scope="/lol/summoner/v4").blocked
    assert transport.await_count == 5
    assert set(breaker._endpoint_breakers) == {"/lol/summoner/v4", "/lol/league/v4"}


async def test_unauthorized_api_family_still_pauses_all_riot_apis() -> None:
    breaker = RiotAuthBreaker()
    middleware = riot_auth_breaker_middleware(breaker)(AsyncMock(return_value=RiotResponse(401)))
    await middleware(Invocation("GET", "https://example.invalid/lol/summoner/v4/item", {}))
    assert not breaker.can_attempt(scope=riot.RIOT_LEAGUE_AUTH_SCOPE)
    with pytest.raises(RiotAuthCircuitOpen):
        await middleware(Invocation("GET", "https://example.invalid/lol/league/v4/item", {}))


async def test_cancelled_auth_probe_releases_both_gates() -> None:
    import asyncio

    now = [100.0]
    breaker = RiotAuthBreaker(clock=lambda: now[0])
    scope = riot.RIOT_LEAGUE_AUTH_SCOPE
    breaker.record_status(401, was_probe=False)
    for _ in range(riot.RIOT_FORBIDDEN_THRESHOLD):
        breaker.for_scope(scope).record_status(403, was_probe=False)
    assert breaker.snapshot(scope=scope).blocked
    now[0] += 900
    transport = AsyncMock(side_effect=asyncio.CancelledError)
    middleware = riot_auth_breaker_middleware(breaker)(transport)
    with pytest.raises(asyncio.CancelledError):
        await middleware(Invocation("GET", "https://example.invalid/lol/league/v4/item", {}))
    assert not breaker.snapshot().probe_in_flight
    assert not breaker.snapshot(scope=scope).probe_in_flight
    now[0] += 900
    assert breaker.can_attempt(scope=scope)


async def test_forbidden_recovery_probe_does_not_hold_global_credential_gate() -> None:
    now = [100.0]
    breaker = RiotAuthBreaker(clock=lambda: now[0])
    breaker.record_status(401, was_probe=False)
    now[0] += 900
    middleware = riot_auth_breaker_middleware(breaker)(AsyncMock(return_value=RiotResponse(403)))
    await middleware(Invocation("GET", "https://example.invalid/lol/summoner/v4/item", {}))
    # The 403 probe closes the credential-wide gate; the endpoint needs more 403s.
    assert not breaker.snapshot().blocked
    assert breaker.can_attempt(scope="/lol/summoner/v4")
    for _ in range(riot.RIOT_FORBIDDEN_THRESHOLD - 1):
        await middleware(Invocation("GET", "https://example.invalid/lol/summoner/v4/item", {}))
    assert breaker.can_attempt(scope=riot.RIOT_LEAGUE_AUTH_SCOPE)
    assert not breaker.can_attempt(scope="/lol/summoner/v4")


def test_auth_breaker_ignores_late_success_but_closes_on_non_auth_probe_response() -> None:
    now = [100.0]
    breaker = RiotAuthBreaker(probe_interval_seconds=900, clock=lambda: now[0])
    breaker.record_status(401, was_probe=False)

    breaker.record_status(200, was_probe=False)
    assert breaker.snapshot().blocked

    now[0] += 900
    probe = breaker.acquire()
    breaker.record_status(404, was_probe=probe)
    assert not breaker.snapshot().blocked


@pytest.mark.parametrize("status", [429, 503])
async def test_riot_api_call_does_not_repeat_client_retries(status: int) -> None:
    operation = AsyncMock(side_effect=RiotResponseError(status))

    with pytest.raises(RiotAPIUnavailable) as raised:
        await riot_api_call(operation)

    # The original aiohttp error text holds the formatted URL; do not chain it.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert raised.value.status == status
    operation.assert_awaited_once_with()


async def test_riot_api_call_does_not_retry_client_error() -> None:
    operation = AsyncMock(side_effect=RiotResponseError(401))

    with pytest.raises(RiotAPIUnavailable):
        await riot_api_call(operation)

    operation.assert_awaited_once_with()


async def test_riot_client_bounds_transport_and_closes_its_session() -> None:
    client = riot.create_riot_api_client("test-only")
    async with client:
        session = client.session
        assert session is not None
        assert session.timeout.total == 10
        assert session.timeout.connect == 5
        assert session.timeout.sock_read == 8
        with pytest.raises(RuntimeError, match="already open"):
            await client.__aenter__()
    assert session.closed
    assert client.session is None


async def test_riot_client_stalled_response_exhausts_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    release = asyncio.Event()

    async def stalled(_request: web.Request) -> web.Response:
        nonlocal attempts
        attempts += 1
        await release.wait()
        return web.json_response([])

    app = web.Application()
    app.router.add_get("/stall", stalled)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(riot, "RIOT_HTTP_TIMEOUT", ClientTimeout(total=0.03))
    retry_sleeps: list[float] = []
    original_sleep = asyncio.sleep
    limiter_time = [1_000_000.0]
    monkeypatch.setattr("pulsefire.ratelimiters.time.time", lambda: limiter_time[0])

    async def fast_backoff(delay: float) -> None:
        retry_sleeps.append(delay)
        limiter_time[0] += delay
        await original_sleep(0)

    monkeypatch.setattr("pulsefire.middlewares.asyncio.sleep", fast_backoff)
    try:
        async with riot.create_riot_api_client("test-only") as client:
            with pytest.raises(RiotAPIUnavailable):
                await asyncio.wait_for(
                    riot_api_call(
                        lambda: client.middleware_begin(
                            Invocation(
                                "GET",
                                str(server.make_url("/stall")),
                                {"region": "EUN1"},
                                session=client.session,
                            )
                        )
                    ),
                    timeout=2,
                )
        assert attempts == 2
        assert [delay for delay in retry_sleeps if delay >= 1] == [2]
        # The timed out attempt released its limiter lease, so the retry did not
        # poll the "pinging" limits for 10 seconds.
        assert 0.1 not in retry_sleeps
    finally:
        release.set()
        await server.close()
