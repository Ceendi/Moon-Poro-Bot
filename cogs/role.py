import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get
from typing import Optional
import datetime
import config
from config import warn_days, warns
import functions


class Role(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.checks.has_any_role("Administracja", 'Moderacja')
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="dodaj_role", description="Dodaje rolę dla danego użytkownika.")
    @app_commands.describe(
        uzytkownik = "Osoba, której dodajesz rolę.",
        role1 = "Rola, którą chcesz dodać.",
        role2 = "Druga, opcjonalna rola.",
        role3 = "Trzecia, opcjonalna rola.",
        role4 = "Czwarta, opcjonalna rola.",
        role5 = "Piąta, opcjonalna rola."
    )
    async def daj_role(self, interaction: discord.Interaction, uzytkownik: discord.Member, role1: discord.Role,
    role2: Optional[discord.Role], role3: Optional[discord.Role], role4: Optional[discord.Role], role5: Optional[discord.Role]):
        roles = [role1, role2, role3, role4, role5]
        failed_roles = []
        succesful_roles = []
        for role in roles:
            if role and str(role) in (config.lol_other + config.lol_ranks + config.lol_servers + ["Użytkownik", "Nie posiadam konta w lolu", "Valorant", "LOR", "TFT"]):
                if role not in uzytkownik.roles:
                    if str(role) in config.lol_ranks and functions.has_rank_roles(uzytkownik):
                        for rank_role in uzytkownik.roles:
                            if str(rank_role) in config.lol_ranks:
                                await uzytkownik.remove_roles(rank_role)
                                break
                    await uzytkownik.add_roles(role)
                    succesful_roles.append("**"+str(role)+"**")
            elif role:
                failed_roles.append("**"+str(role)+"**")
        if not failed_roles:
            await interaction.response.send_message("Dla " + uzytkownik.mention + " dodano role: " + ', '.join(succesful_roles) + ".")
        else:
            message = "Dla " + uzytkownik.mention + " dodano role: " + ', '.join(succesful_roles) + ".\nNie udało się dodać(brak uprawnień): " + ', '.join(failed_roles) + "."
            await interaction.response.send_message(message)

    @app_commands.checks.has_any_role("Administracja", 'Moderacja')
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="usun_role", description="Usuwa rolę dla danego użytkownika.")
    @app_commands.describe(
        uzytkownik = "Osoba, której usuwasz rolę.",
        role1 = "Rola, którą chcesz usunąć.",
        role2 = "Druga, opcjonalna rola.",
        role3 = "Trzecia, opcjonalna rola.",
        role4 = "Czwarta, opcjonalna rola.",
        role5 = "Piąta, opcjonalna rola."
    )
    async def usun_role(self, interaction: discord.Interaction, uzytkownik: discord.Member, role1: discord.Role,
    role2: Optional[discord.Role], role3: Optional[discord.Role], role4: Optional[discord.Role], role5: Optional[discord.Role]):
        warn_values = {"Warn": 1, "Warn 2": 2, "TIMEOUT": 3}
        roles = [role1, role2, role3, role4, role5]
        failed_roles = []
        succesful_roles = []
        for role in roles:
            if role and str(role) in (config.lol_other + config.lol_ranks + config.lol_servers + ["Użytkownik", "Nie posiadam konta w lolu", "Valorant", "LOR", "TFT"]):
                if role in uzytkownik.roles:
                    await uzytkownik.remove_roles(role)
                    succesful_roles.append("**"+str(role)+"**")
            elif role and str(role) in ["Warn", "Warn 2", "TIMEOUT"]:
                data = await self.bot.pool.fetch("SELECT * FROM warn WHERE id=$1;", uzytkownik.id)
                if data:
                    data = data[0]
                    warn_channel = interaction.guild.get_channel(config.warn_channel_id)
                    message = warn_channel.get_partial_message(data["message_id"])
                    old_warn_role = get(interaction.guild.roles, name=warns[data["typ"]])
                    role_value = warn_values[str(role)]
                    if data["typ"] - role_value <= 0:
                        await message.delete()
                        await self.bot.pool.execute("DELETE FROM warn WHERE id=$1;", uzytkownik.id)
                        await uzytkownik.remove_roles(old_warn_role)
                        succesful_roles.append("**"+str(role)+"**")
                    else:
                        new_warn_value = data["typ"] - role_value
                        new_warn_type = warns[new_warn_value]
                        new_warn_role = get(interaction.guild.roles, name=new_warn_type)
                        koniec = data["koniec"] - datetime.timedelta(days=warn_days[str(role)]) #do zmiany
                        embed = discord.Embed(title=str(new_warn_role), description=data["powod"] + " punkt regulaminu", colour=discord.Colour.red())
                        embed.add_field(name="Data otrzymania", value="<t:"+str(data["start"].timestamp())[:-2]+":F>")
                        embed.add_field(name="Data zakończenia", value="<t:"+str(koniec.timestamp())[:-2]+":F>")
                        embed.add_field(name="Użytkownik", value=uzytkownik.mention, inline=False)
                        for autor in data["autorzy"]:
                            embed.add_field(name="Mod", value="<@"+str(autor)+">")
                        await message.edit(embed=embed)
                        await self.bot.pool.execute("UPDATE warn SET typ=$1, koniec=$2 WHERE id=$3", new_warn_value, koniec, uzytkownik.id)
                        await uzytkownik.remove_roles(old_warn_role)
                        await uzytkownik.add_roles(new_warn_role)
                        succesful_roles.append("**"+str(role)+"**")
                else:
                    failed_roles.append("**"+str(role)+"**")
            elif role:
                failed_roles.append("**"+str(role)+"**")
        if not failed_roles:
            await interaction.response.send_message('Dla ' + uzytkownik.mention + ' usunięto role: ' + ', '.join(succesful_roles) + '.')
        else:
            message = "Dla " + uzytkownik.mention + " usunięto role: " + ', '.join(succesful_roles) + ".\nNie udało się usunąć(brak uprawnień lub błąd w przypadku warna): " + ', '.join(failed_roles) + "."
            await interaction.response.send_message(message)

async def setup(bot: commands.Bot):
    await bot.add_cog(Role(bot), guild = discord.Object(id = config.guild_id))