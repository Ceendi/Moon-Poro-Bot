from __future__ import annotations

from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from moon_poro.bot import MoonPoroBot
from moon_poro.permissions import administrator_only


class StatsPaginator(discord.ui.View):
    def __init__(
        self,
        entries: dict[tuple[int, int], dict[int, tuple[int, int]]],
        guild: discord.Guild,
        owner_id: int,
        timeout: int,
    ) -> None:
        super().__init__(timeout=timeout)
        self.entries = entries
        self.periods = sorted(entries)
        self.page = len(self.periods) - 1
        self.guild = guild
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("To nie jest Twój paginator.", ephemeral=True)
        return False

    def embed(self) -> discord.Embed:
        year, month = self.periods[self.page]
        embed = discord.Embed(
            title=f"📊 Statystyki moderacji — {year:04d}-{month:02d}",
            description="**Format:** historyczne zgłoszenia | warny",
            colour=discord.Colour.blue(),
        )
        for user_id, (reports, warnings) in sorted(self.entries[(year, month)].items()):
            if reports == 0 and warnings == 0:
                continue
            member = self.guild.get_member(user_id)
            name = member.mention if member else f"ID: {user_id}"
            embed.add_field(name=name, value=f"{reports} | {warnings}", inline=False)
        embed.set_footer(text=f"Strona {self.page + 1}/{len(self.periods)}")
        return embed

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.blurple)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.blurple)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(len(self.periods) - 1, self.page + 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)


class ModStatsCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="mod_stats", description="Pokazuje miesięczne statystyki moderacji")
    async def mod_stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self.bot.moderation_stats.list_for_guild(interaction.guild_id or 0)
        entries: dict[tuple[int, int], dict[int, tuple[int, int]]] = defaultdict(dict)
        for row in rows:
            entries[(row.year, row.month)][row.moderator_id] = (
                row.reports_count,
                row.warnings_count,
            )
        if not entries or interaction.guild is None:
            await interaction.followup.send("Brak danych do wyświetlenia.", ephemeral=True)
            return
        view = StatsPaginator(
            dict(entries),
            interaction.guild,
            interaction.user.id,
            self.bot.settings.view_timeout,
        )
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(ModStatsCog(bot))
