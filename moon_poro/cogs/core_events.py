from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from moon_poro.bot import MoonPoroBot
from moon_poro.permissions import administrator_only

ACCOUNT_AGE_FEATURE = "account_age_gate"


class CoreEventsCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot

    async def _account_age_gate_enabled(self, guild_id: int) -> bool:
        return await self.bot.guild_features.get(
            guild_id, ACCOUNT_AGE_FEATURE, self.bot.settings.account_age_gate_enabled
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not await self._account_age_gate_enabled(member.guild.id):
            return
        age = datetime.now(UTC) - member.created_at
        required_age = timedelta(days=self.bot.settings.minimum_account_age_days)
        if age >= required_age:
            return
        await member.ban(
            reason=(
                "Automatyczna ochrona: konto młodsze niż "
                f"{self.bot.settings.minimum_account_age_days} dni"
            ),
            delete_message_seconds=0,
        )
        await self._log(
            member.guild,
            f"🔨 Zbanowano użytkownika `{member}` (`{member.id}`): zbyt młode konto Discord.",
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._log(
            member.guild,
            f"👋 Użytkownik `{member}` (`{member.id}`) opuścił serwer lub został usunięty.",
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self._log(guild, f"🔨 Użytkownik `{user}` (`{user.id}`) został zbanowany.")

    async def _log(self, guild: discord.Guild, content: str) -> None:
        settings = self.bot.settings
        if not settings.member_logs_enabled or settings.komendy_botowe_channel_id is None:
            return
        channel = guild.get_channel(settings.komendy_botowe_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(
        name="wylacz_multikonta",
        description="Włącza lub wyłącza ochronę przed młodymi kontami Discord",
    )
    async def toggle_account_age_gate(self, interaction: discord.Interaction) -> None:
        current = await self._account_age_gate_enabled(interaction.guild_id or 0)
        new_value = not current
        await self.bot.guild_features.set(
            interaction.guild_id or 0, ACCOUNT_AGE_FEATURE, new_value, interaction.user.id
        )
        status = "włączona" if new_value else "wyłączona"
        await interaction.response.send_message(
            f"Ochrona przed młodymi kontami jest teraz **{status}**.", ephemeral=True
        )


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(CoreEventsCog(bot))
