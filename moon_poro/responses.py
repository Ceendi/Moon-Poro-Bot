from __future__ import annotations

import logging
import traceback

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("moon_poro.errors")


async def safe_send(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.HTTPException:
        logger.exception("Could not respond to an interaction")


async def handle_known_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> bool:
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.CommandOnCooldown):
        await safe_send(interaction, f"⏳ Spróbuj ponownie za {int(error.retry_after)} s.")
        return True
    if isinstance(error, app_commands.CheckFailure):
        await safe_send(interaction, "❌ Nie masz uprawnień do użycia tej komendy.")
        return True
    if isinstance(original, discord.Forbidden):
        await safe_send(interaction, "❌ Bot nie ma wymaganych uprawnień Discorda.")
        return True
    return False


def install_error_handler(bot: commands.Bot) -> None:
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if await handle_known_error(interaction, error):
            return
        logger.error("Unhandled command error: %s\n%s", error, traceback.format_exc())
        await safe_send(interaction, "❌ Wystąpił nieoczekiwany błąd. Spróbuj ponownie później.")
