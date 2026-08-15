from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast, overload

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
from pulsefire.ratelimiters import RiotAPIRateLimiter

from moon_poro.roles import find_role
from moon_poro.settings import Settings

logger = logging.getLogger("moon_poro.riot")

DEFAULT_RETRY_AFTER_SECONDS = 1.0

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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe_interval_seconds = probe_interval_seconds
        self._clock = clock
        self._blocked = False
        self._last_status: int | None = None
        self._next_probe_at = 0.0
        self._probe_in_flight = False

    def can_attempt(self) -> bool:
        return not self._blocked or (
            not self._probe_in_flight and self._clock() >= self._next_probe_at
        )

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
        if status in {401, 403}:
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

    def snapshot(self) -> RiotAuthBreakerSnapshot:
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


def riot_auth_breaker_middleware(auth_breaker: RiotAuthBreaker) -> Middleware:
    def constructor(next_call: MiddlewareCallable) -> MiddlewareCallable:
        async def middleware(invocation: Invocation) -> Any:
            was_probe = auth_breaker.acquire()
            try:
                response = await next_call(invocation)
            except Exception:
                auth_breaker.record_probe_exception(was_probe=was_probe)
                raise
            status = getattr(response, "status", None)
            if isinstance(status, int):
                auth_breaker.record_status(status, was_probe=was_probe)
            return response

        return middleware

    return constructor


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
    return RiotAPIClient(
        default_headers={"X-Riot-Token": api_token},
        middlewares=[
            json_response_middleware(),
            http_error_middleware(),
            rate_limiter_middleware(RiotAPIRateLimiter()),
            # Keep the shared gate closest to the transport so a request delayed by
            # Pulsefire's adaptive limiter checks the breaker immediately before I/O.
            riot_circuit_breaker_middleware(shared_breaker),
            riot_auth_breaker_middleware(shared_auth_breaker),
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
            if not_found is _NOT_FOUND_UNSET:
                raise RiotAPINotFound(status=404) from error
            return cast(T | None, not_found)
        if isinstance(error, RiotAPIUnavailable):
            raise
        headers: Mapping[str, str] = getattr(error, "headers", {})
        retry_after = _retry_after_seconds(headers) if status == 429 else None
        logger.exception("Riot API request failed with status %s", status)
        raise RiotAPIUnavailable(status=status, retry_after=retry_after) from error


def get_rank_from_leagues(leagues: list[dict[str, Any]]) -> str:
    for league in leagues:
        if league.get("queueType") == "RANKED_SOLO_5x5":
            return str(league.get("tier", "UNRANKED"))
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
