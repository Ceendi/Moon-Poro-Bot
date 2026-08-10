from __future__ import annotations

import discord
from discord.ext import commands

from moon_poro.bot import MoonPoroBot
from moon_poro.permissions import is_moderator


class ReviewedView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_moderator(interaction):
            return True
        await interaction.response.send_message(
            "Tylko moderacja może oznaczyć alert.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="✓ Sprawdzone",
        style=discord.ButtonStyle.green,
        custom_id="moderation:reviewed:v1",
    )
    async def reviewed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)


class MessageModerationCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        bot.add_view(ReviewedView())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        content = message.content.casefold()
        settings = self.bot.settings

        if (
            settings.clash_filter_enabled
            and settings.szukanie_gry_channel_id == message.channel.id
            and "clash" in content
        ):
            await self._handle_clash(message)
            return

        if settings.boost_alert_enabled and any(
            keyword.casefold() in content for keyword in settings.boost_keywords
        ):
            await self._send_boost_alert(message)

    async def _handle_clash(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.Forbidden:
            return
        try:
            await message.author.send(
                "Na tym kanale nie szukamy graczy do Clash. Skorzystaj z kanału przeznaczonego do Clash."
            )
        except discord.Forbidden:
            pass

    async def _send_boost_alert(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        channel_id = self.bot.settings.mod_alert_channel_id
        channel = message.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            return
        embed = discord.Embed(title="⚠️ Możliwa oferta boostingu", colour=discord.Colour.orange())
        embed.add_field(
            name="Autor", value=f"{message.author} (`{message.author.id}`)", inline=False
        )
        channel_name = getattr(message.channel, "mention", f"ID: {message.channel.id}")
        embed.add_field(name="Kanał", value=channel_name, inline=False)
        embed.add_field(name="Wiadomość", value=message.content[:1000] or "(pusta)", inline=False)
        embed.add_field(name="Link", value=message.jump_url, inline=False)
        await channel.send(
            embed=embed,
            view=ReviewedView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(MessageModerationCog(bot))
