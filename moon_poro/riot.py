from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from string import Formatter
from typing import Any, Self, cast, overload
from urllib.parse import quote, urlsplit

import aiohttp
import discord
from pulsefire.clients import RiotAPIClient
from pulsefire.invocation import Invocation
from pulsefire.middlewares import (
    Middleware,
    MiddlewareCallable,
    http_error_middleware,
    json_response_middleware,
    rate_limiter_middleware,
)
from pulsefire.ratelimiters import BaseRateLimiter, RiotAPIRateLimiter

from moon_poro.rank_refresh import SOLO_QUEUE, InvalidLeagueEntries, validate_league_entries
from moon_poro.roles import find_role
from moon_poro.settings import Settings

logger = logging.getLogger("moon_poro.riot")

DEFAULT_RETRY_AFTER_SECONDS = 1.0
# Worst case for one call: (RIOT_HTTP_MAX_RETRIES + 1) * total plus 2 s of
# backoff, so about 22 s. Rate limiter and Retry-After waits come on top of it.
RIOT_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5, sock_read=8)
RIOT_HTTP_MAX_RETRIES = 1
RIOT_LEAGUE_AUTH_SCOPE = "/lol/league/v4"
RIOT_LEAGUE_ENTRIES_PATH = "/lol/league/v4/entries/by-puuid/{puuid}"
# Riot's gateway answers unknown or malformed paths with 403, so a single 403 is
# not proof that the key lost access to a whole API family.
RIOT_FORBIDDEN_THRESHOLD = 3

SERVER_TRANSLATION = {"EUN1": "EUNE", "EUW1": "EUW", "NA1": "NA"}
SERVERS = {"eune": "EUN1", "euw": "EUW1", "na": "NA1"}
API_SERVERS = {
    "eune": "europe",
    "euw": "europe",
    "na": "americas",
    "EUN1": "europe",
    "EUW1": "europe",
    "NA1": "americas",
}
RANK_TO_ROLE = {
    "IRON": "Iron",
    "BRONZE": "Bronze",
    "SILVER": "Silver",
    "GOLD": "Gold",
    "PLATINUM": "Platinum",
    "EMERALD": "Emerald",
    "DIAMOND": "Diamond",
    "MASTER": "Master",
    "GRANDMASTER": "GrandMaster",
    "CHALLENGER": "Challenger",
    "UNRANKED": "Unranked",
}


class RiotAPIUnavailable(RuntimeError):
    def __init__(self, *, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(f"Riot API request failed with status {status}")
        self.status = status
        self.retry_after = retry_after


class RiotAPINotFound(RiotAPIUnavailable):
    pass


class RiotAuthCircuitOpen(RiotAPIUnavailable):
    pass


class RiotAPIInvalidResponse(RiotAPIUnavailable):
    """A successful response whose body cannot be trusted; retry it like an outage."""

    def __init__(self, reason: str) -> None:
        super().__init__(status=None)
        self.args = (f"Riot API returned an invalid response: {reason}",)


@dataclass(frozen=True, slots=True)
class RiotAPIMetricsSnapshot:
    responses_429: int
    responses_401: int
    responses_403: int
    responses_5xx: int
    last_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class RiotAuthBreakerSnapshot:
    blocked: bool
    last_status: int | None
    retry_after_seconds: float
    probe_in_flight: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RiotAPIMonitor:
    """Process-local counters for raw Riot HTTP responses."""

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._responses_429 = 0
        self._responses_401 = 0
        self._responses_403 = 0
        self._responses_5xx = 0
        self._last_success_at: datetime | None = None

    def record_status(self, status: int) -> None:
        if 200 <= status < 300:
            self._last_success_at = self._clock()
        elif status == 429:
            self._responses_429 += 1
        elif status == 401:
            self._responses_401 += 1
        elif status == 403:
            self._responses_403 += 1
        elif 500 <= status < 600:
            self._responses_5xx += 1

    def snapshot(self) -> RiotAPIMetricsSnapshot:
        return RiotAPIMetricsSnapshot(
            responses_429=self._responses_429,
            responses_401=self._responses_401,
            responses_403=self._responses_403,
            responses_5xx=self._responses_5xx,
            last_success_at=self._last_success_at,
        )


class RiotCircuitBreaker:
    """Pause every Riot request sharing a routing region after HTTP 429."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._blocked_until: dict[str, float] = {}

    @staticmethod
    def _key(region: str) -> str:
        return region.strip().lower() or "unknown"

    def block(self, region: str, retry_after: float) -> None:
        key = self._key(region)
        deadline = self._clock() + max(0.0, retry_after)
        previous_deadline = self._blocked_until.get(key, 0.0)
        if deadline <= previous_deadline:
            return
        self._blocked_until[key] = deadline
        logger.warning(
            "Riot API circuit breaker opened for region %s for %.1f seconds",
            key,
            retry_after,
        )

    def retry_after(self, region: str) -> float:
        key = self._key(region)
        deadline = self._blocked_until.get(key, 0.0)
        remaining = deadline - self._clock()
        if remaining > 0:
            return remaining
        self._blocked_until.pop(key, None)
        return 0.0

    async def wait(self, region: str) -> None:
        while (delay := self.retry_after(region)) > 0:
            await self._sleep(delay)


class RiotAuthBreaker:
    """Stop a bad API credential from consuming the whole refresh queue."""

    def __init__(
        self,
        *,
        probe_interval_seconds: float = 900,
        forbidden_threshold: int = RIOT_FORBIDDEN_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe_interval_seconds = probe_interval_seconds
        self._forbidden_threshold = max(1, forbidden_threshold)
        self._consecutive_forbidden = 0
        self._clock = clock
        self._blocked = False
        self._last_status: int | None = None
        self._next_probe_at = 0.0
        self._probe_in_flight = False
        self._endpoint_breakers: dict[str, RiotAuthBreaker] = {}

    def for_scope(self, scope: str) -> RiotAuthBreaker:
        """Separate endpoint permissions from failures of the shared credential."""
        breaker = self._endpoint_breakers.get(scope)
        if breaker is None:
            breaker = RiotAuthBreaker(
                probe_interval_seconds=self._probe_interval_seconds,
                forbidden_threshold=self._forbidden_threshold,
                clock=self._clock,
            )
            self._endpoint_breakers[scope] = breaker
        return breaker

    def can_attempt(self, *, scope: str | None = None) -> bool:
        global_allowed = not self._blocked or (
            not self._probe_in_flight and self._clock() >= self._next_probe_at
        )
        endpoint = self._endpoint_breakers.get(scope) if scope is not None else None
        return global_allowed and (endpoint is None or endpoint.can_attempt())

    def acquire(self) -> bool:
        """Return whether this request is the single auth recovery probe."""

        if not self._blocked:
            return False
        retry_after = self._next_probe_at - self._clock()
        if self._probe_in_flight or retry_after > 0:
            raise RiotAuthCircuitOpen(
                status=self._last_status,
                retry_after=max(0.0, retry_after),
            )
        self._probe_in_flight = True
        return True

    def record_status(self, status: int, *, was_probe: bool) -> None:
        if status != 403:
            self._consecutive_forbidden = 0
        elif not self._blocked:
            # A closed gate opens only after several 403s in a row, so one request
            # with a broken path cannot pause the API family for everyone.
            self._consecutive_forbidden += 1
            if self._consecutive_forbidden < self._forbidden_threshold:
                logger.warning(
                    "Riot API returned HTTP 403 (%s of %s in a row before pausing)",
                    self._consecutive_forbidden,
                    self._forbidden_threshold,
                )
                return
        if status in {401, 403}:
            self._consecutive_forbidden = 0
            self._blocked = True
            self._last_status = status
            self._next_probe_at = self._clock() + self._probe_interval_seconds
            self._probe_in_flight = False
            logger.error(
                "Riot auth circuit breaker opened after HTTP %s; next probe in %.0f seconds",
                status,
                self._probe_interval_seconds,
            )
        elif was_probe:
            if self._blocked:
                logger.info(
                    "Riot auth circuit breaker closed after probe received HTTP %s",
                    status,
                )
            self._blocked = False
            self._last_status = None
            self._next_probe_at = 0.0
            self._probe_in_flight = False

    def record_probe_exception(self, *, was_probe: bool) -> None:
        if was_probe:
            self._next_probe_at = self._clock() + self._probe_interval_seconds
            self._probe_in_flight = False

    def snapshot(self, *, scope: str | None = None) -> RiotAuthBreakerSnapshot:
        endpoint = self._endpoint_breakers.get(scope) if scope is not None else None
        if not self._blocked and endpoint is not None:
            return endpoint.snapshot()
        retry_after = max(0.0, self._next_probe_at - self._clock()) if self._blocked else 0.0
        return RiotAuthBreakerSnapshot(
            blocked=self._blocked,
            last_status=self._last_status,
            retry_after_seconds=retry_after,
            probe_in_flight=self._probe_in_flight,
        )


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    raw_value = headers.get("Retry-After")
    try:
        retry_after = float(raw_value) if raw_value is not None else math.nan
    except ValueError:
        retry_after = math.nan
    if not math.isfinite(retry_after) or retry_after < 0:
        logger.warning(
            "Riot API returned HTTP 429 without a valid Retry-After header; using %.1f seconds",
            DEFAULT_RETRY_AFTER_SECONDS,
        )
        return DEFAULT_RETRY_AFTER_SECONDS
    return retry_after


def riot_circuit_breaker_middleware(circuit_breaker: RiotCircuitBreaker) -> Middleware:
    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        async def middleware(invocation: Invocation) -> Any:
            region = str(invocation.params.get("region", "unknown"))
            await circuit_breaker.wait(region)
            response = await next_call(invocation)
            if getattr(response, "status", None) == 429:
                headers: Mapping[str, str] = getattr(response, "headers", {})
                circuit_breaker.block(region, _retry_after_seconds(headers))
            return response

        return middleware

    return constructor


def riot_api_monitoring_middleware(monitor: RiotAPIMonitor) -> Middleware:
    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        async def middleware(invocation: Invocation) -> Any:
            response = await next_call(invocation)
            status = getattr(response, "status", None)
            if isinstance(status, int):
                monitor.record_status(status)
            return response

        return middleware

    return constructor


def _endpoint_template(urlformat: str) -> str:
    """Return the path template, never the formatted URL with a Riot ID or PUUID."""
    return urlsplit(urlformat).path or "/unknown"


def _path_fields(urlformat: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(_endpoint_template(urlformat)) if field}


def riot_path_params_middleware() -> Middleware:
    """Percent-encode string path parameters before Pulsefire formats the URL.

    Pulsefire uses ``str.format`` without encoding, so a Riot ID such as ``ab/cd``
    or ``a?b`` would change the requested path. This runs outside the retry loop
    and works on a copy of the invocation, so a value is never encoded twice.
    """

    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        async def middleware(invocation: Invocation) -> Any:
            params = dict(invocation.params)
            for field in _path_fields(invocation.urlformat):
                value = params.get(field)
                if not isinstance(value, str):
                    continue
                if value in {"", ".", ".."}:
                    # These collapse into another path and match no Riot account.
                    raise RiotAPINotFound(status=404)
                params[field] = quote(value, safe="")
            encoded = Invocation(
                invocation.method,
                invocation.urlformat,
                params,
                invocation.session,
                invoker=invocation.invoker,
                uid=invocation.uid,
            )
            try:
                return await next_call(encoded)
            except Exception as error:
                # Lets riot_api_call log the endpoint without the formatted URL.
                try:
                    error.riot_endpoint = _endpoint_template(invocation.urlformat)  # type: ignore[attr-defined]
                except AttributeError:
                    pass
                raise

        return middleware

    return constructor


def riot_league_payload_middleware() -> Middleware:
    """Reject a League-v4 200 that is not a list of entries with valid tiers.

    Pulsefire returns raw bytes when JSON parsing fails. Reading that, or any
    other unexpected shape, as "no Solo/Duo entry" could mark a player Unranked.
    """

    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        async def middleware(invocation: Invocation) -> Any:
            payload = await next_call(invocation)
            if _endpoint_template(invocation.urlformat) != RIOT_LEAGUE_ENTRIES_PATH:
                return payload
            try:
                return validate_league_entries(payload)
            except InvalidLeagueEntries as error:
                logger.warning("Riot League-v4 returned an invalid payload: %s", error)
                raise RiotAPIInvalidResponse(str(error)) from None

        return middleware

    return constructor


def riot_rate_limiter_middleware(rate_limiter: BaseRateLimiter) -> Middleware:
    """Pulsefire's limiter, releasing its header lease when no response arrives.

    Pulsefire synchronizes only after a response. A transport timeout or a
    short-circuited request would otherwise leave the app and method limits
    "pinging", and every other request for that region would poll for 10 s.
    """

    limited = rate_limiter_middleware(rate_limiter)

    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        inner = limited(next_call)

        async def middleware(invocation: Invocation) -> Any:
            try:
                return await inner(invocation)
            except BaseException:
                try:
                    # Without rate limit headers the limiter drops the pending
                    # discovery, so the next request measures the limits again.
                    await rate_limiter.synchronize(invocation, {})
                except Exception:  # pragma: no cover - the local limiter does not raise.
                    logger.warning("Could not release the Riot rate limiter lease")
                raise

        return middleware

    return constructor


def riot_auth_breaker_middleware(auth_breaker: RiotAuthBreaker) -> Middleware:
    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        async def middleware(invocation: Invocation) -> Any:
            # Use the fixed API-family path, never a formatted URL containing a
            # user's PUUID or Riot ID. A forbidden endpoint must not stop other APIs.
            parts = [part for part in _endpoint_template(invocation.urlformat).split("/") if part]
            scope = "/" + "/".join(parts[:3]) if parts else "/unknown"
            endpoint = auth_breaker.for_scope(scope)
            endpoint_probe = endpoint.acquire()
            try:
                was_probe = auth_breaker.acquire()
            except BaseException:
                endpoint.record_probe_exception(was_probe=endpoint_probe)
                raise
            try:
                response = await next_call(invocation)
            except BaseException:
                auth_breaker.record_probe_exception(was_probe=was_probe)
                endpoint.record_probe_exception(was_probe=endpoint_probe)
                raise
            status = getattr(response, "status", None)
            if isinstance(status, int):
                # 401 is credential-wide. 403 is permission/resource-specific;
                # an invalid credential returning 403 closes each used gate after
                # a few requests, without sending requests for every queued user.
                auth_breaker.record_status(404 if status == 403 else status, was_probe=was_probe)
                endpoint.record_status(500 if status == 401 else status, was_probe=endpoint_probe)
            return response

        return middleware

    return constructor


class _BoundedRiotAPIClient(RiotAPIClient):  # type: ignore[misc]  # Pulsefire has no typed client API.
    """Bound transport stalls, not the rate limiter or Retry-After wait."""

    session: aiohttp.ClientSession | None

    async def __aenter__(self) -> Self:
        if self.session is not None:
            raise RuntimeError("Riot client is already open")
        self.session = aiohttp.ClientSession(timeout=RIOT_HTTP_TIMEOUT)
        return self


def create_riot_api_client(
    api_token: str,
    *,
    circuit_breaker: RiotCircuitBreaker | None = None,
    auth_breaker: RiotAuthBreaker | None = None,
    monitor: RiotAPIMonitor | None = None,
) -> RiotAPIClient:
    shared_breaker = circuit_breaker or RiotCircuitBreaker()
    shared_auth_breaker = auth_breaker or RiotAuthBreaker()
    shared_monitor = monitor or RiotAPIMonitor()
    return _BoundedRiotAPIClient(
        default_headers={"X-Riot-Token": api_token},
        middlewares=[
            riot_league_payload_middleware(),
            # Outside the retry loop, so path parameters are encoded exactly once.
            riot_path_params_middleware(),
            json_response_middleware(),
            http_error_middleware(max_retries=RIOT_HTTP_MAX_RETRIES),
            # Before the limiter: a request refused by an open auth gate never
            # takes a rate limiter lease, so it cannot stall other requests.
            riot_auth_breaker_middleware(shared_auth_breaker),
            riot_rate_limiter_middleware(RiotAPIRateLimiter()),
            # Keep the 429 gate closest to the transport so a request delayed by
            # Pulsefire's adaptive limiter checks it immediately before I/O.
            riot_circuit_breaker_middleware(shared_breaker),
            riot_api_monitoring_middleware(shared_monitor),
        ],
    )


_NOT_FOUND_UNSET = object()


@overload
async def riot_api_call[T](
    operation: Callable[[], Awaitable[T]],
    *,
    not_found: T | None,
) -> T | None: ...


@overload
async def riot_api_call[T](operation: Callable[[], Awaitable[T]]) -> T: ...


async def riot_api_call[T](
    operation: Callable[[], Awaitable[T]],
    *,
    not_found: T | object | None = _NOT_FOUND_UNSET,
) -> T | None:
    """Run one Pulsefire operation; its middleware owns the retry policy."""
    try:
        return await operation()
    except Exception as error:
        status = getattr(error, "status", None)
        if status == 404:
            if not_found is not _NOT_FOUND_UNSET:
                return cast(T | None, not_found)
            if isinstance(error, RiotAPINotFound):
                raise
            raise RiotAPINotFound(status=404) from None
        if isinstance(error, RiotAPIUnavailable):
            raise
        headers: Mapping[str, str] = getattr(error, "headers", {})
        retry_after = _retry_after_seconds(headers) if status == 429 else None
        # aiohttp's error text and traceback carry the formatted URL, which holds
        # the player's Riot ID or PUUID. Log the endpoint template instead, and
        # keep a traceback only for errors that are neither HTTP nor transport.
        transport_error = isinstance(error, (aiohttp.ClientError, TimeoutError))
        logger.warning(
            "Riot API request to %s failed with status %s (%s)",
            getattr(error, "riot_endpoint", "unknown endpoint"),
            status,
            type(error).__name__,
            exc_info=None if transport_error else error,
        )
        raise RiotAPIUnavailable(status=status, retry_after=retry_after) from None


def get_rank_from_leagues(leagues: list[dict[str, Any]]) -> str:
    """Return the Solo/Duo tier; raise ``RiotAPIInvalidResponse`` on malformed entries."""
    try:
        entries = validate_league_entries(leagues, allow_unranked=True)
    except InvalidLeagueEntries as error:
        raise RiotAPIInvalidResponse(str(error)) from None
    for league in entries:
        if league.get("queueType") == SOLO_QUEUE:
            return str(league["tier"]).upper()
    return "UNRANKED"


def get_discord_rank_role(
    guild: discord.Guild, rank: str, settings: Settings
) -> discord.Role | None:
    role_name = RANK_TO_ROLE.get(rank.upper(), "Unranked")
    return find_role(guild, role_name, settings)


def profile_icon_url(icon_id: int) -> str:
    return (
        "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
        f"global/default/v1/profile-icons/{icon_id}.jpg"
    )
