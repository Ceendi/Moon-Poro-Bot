import logging
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger('discord.errors')


class BotError(Exception):
    def __init__(self, message: str, user_message: Optional[str] = None):
        self.message = message
        self.user_message = user_message or "Wystąpił nieoczekiwany błąd. Spróbuj ponownie później."
        super().__init__(self.message)


class RiotAPIError(BotError):
    pass


class UserNotFoundError(BotError):
    pass


async def handle_app_command_error(
    interaction: discord.Interaction, 
    error: app_commands.AppCommandError
) -> bool:
    if isinstance(error, app_commands.MissingAnyRole):
        await safe_send(
            interaction, 
            "❌ Nie posiadasz uprawnień do użycia tej komendy.",
            ephemeral=True
        )
        return True
    
    if isinstance(error, app_commands.CommandOnCooldown):
        await safe_send(
            interaction,
            f"⏳ Poczekaj {int(error.retry_after)}s przed ponownym użyciem.",
            ephemeral=True
        )
        return True
    
    if isinstance(error, app_commands.CheckFailure):
        await safe_send(
            interaction,
            "❌ Nie spełniasz wymagań do użycia tej komendy.",
            ephemeral=True
        )
        return True
    
    return False


async def safe_send(
    interaction: discord.Interaction, 
    content: str, 
    ephemeral: bool = True,
    **kwargs
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
    except discord.HTTPException as e:
        logger.error(f"Failed to send response: {e}")


def setup_error_handler(bot: commands.Bot) -> None:
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, 
        error: app_commands.AppCommandError
    ):
        if await handle_app_command_error(interaction, error):
            return
        
        logger.error(f"Unhandled error in {interaction.command}: {error}")
        logger.error(traceback.format_exc())
        
        await safe_send(
            interaction,
            "❌ Wystąpił nieoczekiwany błąd. Spróbuj ponownie później.",
            ephemeral=True
        )


def create_permission_error_handler(cog_name: str):
    async def error_handler(
        self, 
        interaction: discord.Interaction, 
        error: app_commands.AppCommandError
    ):
        if not await handle_app_command_error(interaction, error):
            raise error
    
    return error_handler
