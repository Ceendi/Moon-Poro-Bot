from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from moon_poro.roles import find_role
from moon_poro.settings import Settings

logger = logging.getLogger("moon_poro.riot")

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
