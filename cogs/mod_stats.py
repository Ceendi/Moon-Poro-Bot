import discord
from discord import app_commands
from discord.ext import commands
import datetime
import config


class Paginator(discord.ui.View):
    def __init__(self, entries: list, bot):
        super().__init__(timeout=180)
        self.entries: dict = entries
        self.current_page = 0
        self.max_page = len(self.entries.keys())
        self.bot: commands.Bot() = bot
    
    def switch_page(self, count: int) -> list:
        self.current_page += count

        if self.current_page < 0:
            self.current_page = 0
        elif self.current_page >= self.max_page:
            self.current_page = self.max_page - 1
        return self.entries[list(self.entries)[self.current_page]]

    def format_page(self, entries: dict):
        embed = discord.Embed(title='Zgloszenia|Warny')
        id = list(self.entries)[self.current_page]
        user = self.bot.get_user(id)
        if user:
            embed.add_field(name="Mod", value=str(user), inline=False)
        else:
            embed.add_field(name="Mod", value=str(id), inline=False)
        month = datetime.datetime.now().month + 1
        for value in entries:
            month = month-1
            if month == 0:
                month = 12
            embed.add_field(name=str(month), value='|'.join(map(str, value)))
        return embed

    @discord.ui.button(emoji='\U000025c0', style=discord.ButtonStyle.blurple)
    async def on_arrow_backward(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entries = self.switch_page(-1)
        embed = self.format_page(entries)
        return await interaction.response.edit_message(embed=embed)

    @discord.ui.button(emoji='\U000025b6', style=discord.ButtonStyle.blurple)
    async def on_arrow_forward(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entries = self.switch_page(1)
        embed = self.format_page(entries)
        return await interaction.response.edit_message(embed=embed)

    @classmethod
    async def start(cls, interaction: discord.Interaction, entries: dict, bot):
        new = cls(entries, bot)
        embed = new.format_page(entries=entries[list(entries)[0]])
        await interaction.channel.send(embed=embed, view=new)
        return new

class Mod_stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="mod_stats", description="Pokazuje ilosci danych warnow i przyjetych zgloszen przez modow.")
    async def mod_stats(self, interaction: discord.Interaction):
        datas = await self.bot.pool.fetch("SELECT * FROM mod_stats;")
        await Paginator.start(interaction, {data[0]: [x for x in zip(data[1::2], data[2::2])] for data in datas}, self.bot)

async def setup(bot: commands.Bot):
    await bot.add_cog(Mod_stats(bot), guild = discord.Object(id = config.guild_id))