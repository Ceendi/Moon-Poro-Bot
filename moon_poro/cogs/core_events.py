from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

import discord
from discord import app_commands
from discord.ext import commands

from moon_poro.bot import MoonPoroBot
from moon_poro.permissions import administrator_only

ACCOUNT_AGE_FEATURE = "account_age_gate"


class AccountAgeGateAction(StrEnum):
    ENABLE = "on"
    DISABLE = "off"
    CHECK = "check"


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
        name="multikonta",
        description="Włącza, wyłącza lub pokazuje ochronę przed nowymi kontami Discord",
    )
    @app_commands.describe(stan="Co zrobić z ochroną przed multikontami")
    @app_commands.choices(
        stan=[
            app_commands.Choice(name="włącz", value=AccountAgeGateAction.ENABLE.value),
            app_commands.Choice(name="wyłącz", value=AccountAgeGateAction.DISABLE.value),
            app_commands.Choice(name="sprawdź", value=AccountAgeGateAction.CHECK.value),
        ]
    )
    async def account_age_gate(
        self, interaction: discord.Interaction, stan: app_commands.Choice[str]
    ) -> None:
        guild_id = interaction.guild_id or 0
        action = AccountAgeGateAction(stan.value)
        if action is AccountAgeGateAction.CHECK:
            enabled = await self._account_age_gate_enabled(guild_id)
        else:
            enabled = action is AccountAgeGateAction.ENABLE
            await self.bot.guild_features.set(
                guild_id, ACCOUNT_AGE_FEATURE, enabled, interaction.user.id
            )
        await interaction.response.send_message(
            self._account_age_gate_message(
                enabled, changed=action is not AccountAgeGateAction.CHECK
            ),
            ephemeral=True,
        )

    def _account_age_gate_message(self, enabled: bool, *, changed: bool) -> str:
        prefix = "jest teraz" if changed else "jest"
        if not enabled:
            return (
                f"Ochrona przed multikontami {prefix} **wyłączona**. "
                "Bot nie banuje nowych kont Discord przy dołączaniu."
            )
        days = self.bot.settings.minimum_account_age_days
        unit = "dzień" if days == 1 else "dni"
        return (
            f"Ochrona przed multikontami {prefix} **włączona**. "
            f"Bot zbanuje konta Discord młodsze niż {days} {unit} przy dołączaniu."
        )


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(CoreEventsCog(bot))
