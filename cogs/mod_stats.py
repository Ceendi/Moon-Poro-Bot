import discord
from discord import app_commands
from discord.ext import commands
import config

class Mod_stats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    

async def setup(bot: commands.Bot):
    await bot.add_cog(Mod_stats(bot), guild = discord.Object(id = config.guild_id))