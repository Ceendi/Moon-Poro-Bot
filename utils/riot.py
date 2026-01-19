import discord
from discord.utils import get
from typing import Optional, List, Dict, Any

from utils.constants import RANK_TO_ROLE, ROLE_UNRANKED


def get_rank_from_leagues(leagues: List[Dict[str, Any]]) -> str:
    for league in leagues:
        if league.get("queueType") == 'RANKED_SOLO_5x5':
            return league.get("tier", "UNRANKED")
    return "UNRANKED"


def get_discord_rank_role(guild: discord.Guild, rank: str) -> Optional[discord.Role]:
    role_name = RANK_TO_ROLE.get(rank.upper(), ROLE_UNRANKED)
    return get(guild.roles, name=role_name)


def has_role_by_name(member: discord.Member, role_name: str) -> bool:
    return any(role.name == role_name for role in member.roles)


def has_any_role_from_list(member: discord.Member, role_names: List[str]) -> bool:
    member_role_names = {role.name for role in member.roles}
    return bool(member_role_names.intersection(role_names))


def get_roles_to_remove(member: discord.Member, role_names: List[str]) -> List[discord.Role]:
    return [role for role in member.roles if role.name in role_names]
