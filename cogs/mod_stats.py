import discord
from discord import Message, app_commands
from discord.ext import commands
import asyncpg
import config


class Paginator(discord.ui.View):
    def __init__(self, entries: list, bot):
        super().__init__(timeout=180)
        self.entries: dict = entries
        self.current_page = len(self.entries.keys()) - 1
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
        month = list(self.entries)[self.current_page]
        embed = discord.Embed(title=str(month))
        embed.description = "id: zgloszenia|warny"
        for id, values in entries.items():
            user = self.bot.get_user(id)
            if values['z'] == 0 and values['w'] == 0:
                continue
            if user:
                embed.add_field(name='\u200b', value=user.mention+": "+str(values['z'])+"|"+str(values['w']))
            else:
                embed.add_field(name='\u200b', value=str(id)+": "+str(values['z'])+"|"+str(values['w']))
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
    async def start(cls, interaction: discord.Interaction, stats: dict, bot):
        new = cls(stats, bot)
        embed = new.format_page(entries=stats[str(list(stats)[-1])])
        await interaction.channel.send(embed=embed, view=new)
        return new

class Mod_stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.boost_messages = ['boost', 'wbije rangę', 'wbije range', 'bost', 'pomogę z', 'pomoge z',
                          'za free', 'tanio']

    @commands.Cog.listener('on_message')
    async def on_message(self, message: Message):
        if not message.author.bot and any(bm in message.content for bm in self.boost_messages):
            channel = message.guild.get_channel(1199455265234370640)
            await channel.send(f'{message.author.mention} napisał na kanale {message.channel.mention} "{message.content}" {message.id}')

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="mod_stats", description="Pokazuje ilosci danych warnow i przyjetych zgloszen przez modow.")
    async def mod_stats(self, interaction: discord.Interaction):
        datas = await self.bot.pool.fetch("SELECT * FROM mod_stats;")
        data: asyncpg.Record
        stats = dict()
        for data in datas:
            id=data[0]
            for date, number in data.items():
                if date.startswith('id'):
                    id = number
                else:
                    month = date[-2:]
                    try:
                        stats[month][id][date[0]] = number
                    except KeyError:
                        try:
                            stats[month][id] = dict()
                            stats[month][id][date[0]] = number
                        except KeyError:
                            stats[month] = dict()
                            stats[month][id] = dict()
                            stats[month][id][date[0]] = number
        await Paginator.start(interaction, stats, self.bot)

async def setup(bot: commands.Bot):
    await bot.add_cog(Mod_stats(bot), guild = discord.Object(id = config.guild_id))