from sqlite3 import connect
import discord
from discord import app_commands
from discord.ext import commands
import config
from typing import Optional

class Sponsors(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="vban", description="Usuwa dostęp do wbijania na kanał drzez danej osobie.")
    @app_commands.describe(
        uzytkownik1 = "Osoba, której zabierasz dostęp.",
        uzytkownik2 = "Osoba, której zabierasz dostęp.",
        uzytkownik3 = "Osoba, której zabierasz dostęp.",
        uzytkownik4 = "Osoba, której zabierasz dostęp.",
        uzytkownik5 = "Osoba, której zabierasz dostęp."
    )
    async def vban(self, interaction: discord.Interaction, uzytkownik1: discord.Member,
    uzytkownik2: Optional[discord.Member], uzytkownik3: Optional[discord.Member], uzytkownik4: Optional[discord.Member], uzytkownik5: Optional[discord.Member]):
        uzytkownicy = [uzytkownik1, uzytkownik2, uzytkownik3, uzytkownik4, uzytkownik5]
        channel = interaction.guild.get_channel(722904382823334003)
        for uzytkownik in uzytkownicy:
            if uzytkownik:
                if uzytkownik in channel.members:
                    await uzytkownik.move_to(channel=None)
                await channel.set_permissions(uzytkownik, connect=False)
        await interaction.response.send_message("Zabrano dostęp tym osobom!", ephemeral=True)

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="vunban", description="Cofa usunięty dostęp do wbijania na kanał drzez.")
    @app_commands.describe(
        uzytkownik = "Osoba, której oddajesz dostęp."
    )
    async def vunban(self, interaction: discord.Interaction, uzytkownik: discord.Member):
        channel = interaction.guild.get_channel(722904382823334003)
        await channel.set_permissions(uzytkownik, connect=None)
        await interaction.response.send_message("Oddano dostęp do kanału!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Sponsors(bot), guild = discord.Object(id = config.guild_id))