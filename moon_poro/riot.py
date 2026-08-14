from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

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
    pass


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


def create_riot_api_client(
    api_token: str,
    *,
    circuit_breaker: RiotCircuitBreaker | None = None,
) -> RiotAPIClient:
    shared_breaker = circuit_breaker or RiotCircuitBreaker()
    return RiotAPIClient(
        default_headers={"X-Riot-Token": api_token},
        middlewares=[
            json_response_middleware(),
            http_error_middleware(),
            rate_limiter_middleware(RiotAPIRateLimiter()),
            # Keep the shared gate closest to the transport so a request delayed by
            # Pulsefire's adaptive limiter checks the breaker immediately before I/O.
            riot_circuit_breaker_middleware(shared_breaker),
        ],
    )


async def riot_api_call[T](
    operation: Callable[[], Awaitable[T]],
    *,
    not_found: T | None = None,
) -> T | None:
    """Run one Pulsefire operation; its middleware owns the retry policy."""
    try:
        return await operation()
    except Exception as error:
        status = getattr(error, "status", None)
        if status == 404:
            return not_found
        logger.exception("Riot API request failed with status %s", status)
        raise RiotAPIUnavailable from error


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
