import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.constants import BOOST_KEYWORDS
from utils.errors import handle_app_command_error


class SprawdzoneView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✓ Sprawdzone", style=discord.ButtonStyle.green, custom_id="sprawdzone")
    async def sprawdzone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.edit(content=f"~~{interaction.message.content}~~", view=None)
        await interaction.response.defer()


class StatsPaginator(discord.ui.View):
    def __init__(self, entries: dict, bot: commands.Bot):
        super().__init__(timeout=config.VIEW_TIMEOUT)
        self.entries = entries
        self.months = list(entries.keys())
        self.current_page = len(self.months) - 1
        self.bot = bot
    
    def _format_page(self) -> discord.Embed:
        month = self.months[self.current_page]
        data = self.entries[month]
        embed = discord.Embed(title=f"📊 Statystyki - {month}", description="**Format:** Zgłoszenia | Warny")
        for user_id, values in data.items():
            if values['z'] == 0 and values['w'] == 0:
                continue
            user = self.bot.get_user(user_id)
            name = user.mention if user else f"ID: {user_id}"
            embed.add_field(name='\u200b', value=f"{name}: {values['z']} | {values['w']}")
        embed.set_footer(text=f"Strona {self.current_page + 1}/{len(self.months)}")
        return embed

    @discord.ui.button(emoji='◀️', style=discord.ButtonStyle.blurple)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(embed=self._format_page())

    @discord.ui.button(emoji='▶️', style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(len(self.months) - 1, self.current_page + 1)
        await interaction.response.edit_message(embed=self._format_page())


class ModStatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(SprawdzoneView())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        content_lower = message.content.lower()
        if any(keyword in content_lower for keyword in BOOST_KEYWORDS):
            channel = message.guild.get_channel(config.mod_alert_channel_id)
            if channel:
                await channel.send(
                    f"⚠️ {message.author.mention} napisał na {message.channel.mention}:\n\"{message.content}\"\nID: {message.id}",
                    view=SprawdzoneView()
                )

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="mod_stats", description="Pokazuje statystyki moderatorów")
    async def mod_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        datas = await self.bot.pool.fetch("SELECT * FROM mod_stats;")
        stats = {}
        for row in datas:
            user_id = row['id']
            for column, value in row.items():
                if column == 'id':
                    continue
                try:
                    stat_type = column[0]
                    month = column[-2:]
                    if month not in stats:
                        stats[month] = {}
                    if user_id not in stats[month]:
                        stats[month][user_id] = {'z': 0, 'w': 0}
                    stats[month][user_id][stat_type] = value or 0
                except (IndexError, ValueError):
                    continue
        if not stats:
            await interaction.followup.send("Brak danych do wyświetlenia.")
            return
        paginator = StatsPaginator(stats, self.bot)
        await interaction.followup.send(embed=paginator._format_page(), view=paginator)

    @mod_stats.error
    async def mod_stats_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if not await handle_app_command_error(interaction, error):
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(ModStatsCog(bot), guild=discord.Object(id=config.guild_id))