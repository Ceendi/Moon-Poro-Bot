from __future__ import annotations

from collections.abc import Callable

import discord
from discord import app_commands


def is_administrator(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def is_moderator(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    permissions = interaction.user.guild_permissions
    return permissions.administrator or permissions.moderate_members or permissions.manage_messages


def administrator_only[T]() -> Callable[[T], T]:
    return app_commands.check(is_administrator)


def moderator_only[T]() -> Callable[[T], T]:
    return app_commands.check(is_moderator)
