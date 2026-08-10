from __future__ import annotations

import discord

from moon_poro.settings import Settings


def find_role(guild: discord.Guild, role_name: str, settings: Settings) -> discord.Role | None:
    role_id = settings.role_ids.get(role_name)
    if role_id is not None:
        return guild.get_role(role_id)
    return discord.utils.get(guild.roles, name=role_name)


def member_roles_named(
    member: discord.Member,
    names: list[str] | frozenset[str] | set[str],
    settings: Settings,
) -> list[discord.Role]:
    configured_ids = {
        settings.role_ids[name] for name in names if name in settings.role_ids
    }
    fallback_names = {name for name in names if name not in settings.role_ids}
    return [
        role
        for role in member.roles
        if role.id in configured_ids or role.name in fallback_names
    ]


def role_is_configured(role: discord.Role, name: str, settings: Settings) -> bool:
    configured_id = settings.role_ids.get(name)
    return role.id == configured_id if configured_id is not None else role.name == name


def member_has_role(member: discord.Member, name: str, settings: Settings) -> bool:
    configured_id = settings.role_ids.get(name)
    if configured_id is not None:
        return any(role.id == configured_id for role in member.roles)
    return any(role.name == name for role in member.roles)
