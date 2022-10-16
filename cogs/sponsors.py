import discord
from discord import app_commands
from discord.ext import commands
import config
from typing import Optional
from discord.utils import get

class Person(discord.ui.Button):
    def __init__(self, uzytkownik):
        self.uzytkownik: discord.Member = uzytkownik
        super().__init__(label=str(self.uzytkownik), style=discord.ButtonStyle.red)

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(1005927253605093427)
        if self.uzytkownik in channel.members:
            await self.uzytkownik.move_to(channel=None)
        await channel.set_permissions(self.uzytkownik, connect=False)
        await interaction.response.send_message(f"Udało się kicknąć {self.uzytkownik}", ephemeral=True)

class Sponsors(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="vban", description="Usuwa dostęp do wbijania na kanał drzez danej osobie.")
    async def vban(self, interaction: discord.Interaction):
        uzytkownicy = discord.ui.View()
        channel = interaction.guild.get_channel(1005927253605093427)
        if channel.members == []:
            await interaction.response.send_message("Nie znaleziono osób na kanale.", ephemeral=True)
            return
        for uzytkownik in channel.members:
            if uzytkownik.id == 917028904433238036:
                continue
            uzytkownicy.add_item(Person(uzytkownik))
        await interaction.response.send_message(view=uzytkownicy, ephemeral=True)

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="vuban", description="Cofa usunięty dostęp do wbijania na kanał drzez.")
    @app_commands.describe(
        uzytkownik = "Osoba, której oddajesz dostęp."
    )
    async def vunban(self, interaction: discord.Interaction, uzytkownik: discord.Member):
        channel = interaction.guild.get_channel(1005927253605093427)
        await channel.set_permissions(uzytkownik, connect=True)
        await interaction.response.send_message("Oddano dostęp do kanału!", ephemeral=True)

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="vopen", description="Otwiera dostęp do kanału drzez.")
    async def open(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(1005927253605093427)
        uzytkownik = get(interaction.guild.roles, name="Użytkownik")
        await channel.set_permissions(uzytkownik, connect=True, view_channel=True)
        await interaction.response.send_message("Otwarto kanał!", ephemeral=True)

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="vclose", description="Zamyka dostęp do kanału drzez.")
    async def close(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(1005927253605093427)
        uzytkownik = get(interaction.guild.roles, name="Użytkownik")
        await channel.set_permissions(uzytkownik, connect=False, view_channel=True)
        await interaction.response.send_message("Zamknięto kanał!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Sponsors(bot), guild = discord.Object(id = config.guild_id))