import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get
from typing import Optional
import config
from config import warns
import functions


class Role(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.checks.has_any_role("Administracja", 'Moderacja')
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="dr", description="Dodaje rolę dla danego użytkownika.")
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
            if role and str(role) in (config.lol_other + config.lol_ranks + config.lol_servers + ["Użytkownik", "Nie posiadam konta w lolu", "Valorant", "LOR", "TFT", "Wild Rift", "Ogłoszenia", "Lol Newsy"]):
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
            await interaction.response.send_message("Dla " + uzytkownik.mention + " dodano role: " + ', '.join(succesful_roles)) + "."
        else:
            message = "Dla " + uzytkownik.mention + " dodano role: " + ', '.join(succesful_roles) + ".\nNie udało się dodać (brak uprawnień): " + ', '.join(failed_roles) + "."
            await interaction.response.send_message(message)

    @daj_role.error
    async def daj_roleError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
           await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

    @app_commands.checks.has_any_role("Administracja", 'Moderacja')
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="ur", description="Usuwa rolę dla danego użytkownika.")
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
        roles = [role1, role2, role3, role4, role5]
        failed_roles = []
        succesful_roles = []
        for role in roles:
            if role and str(role) in (config.lol_other + config.lol_ranks + config.lol_servers + ["Użytkownik", "Nie posiadam konta w lolu", "Valorant", "LOR", "TFT", "Wild Rift", "Ogłoszenia", "Lol Newsy"]):
                if role in uzytkownik.roles:
                    await uzytkownik.remove_roles(role)
                    succesful_roles.append("**"+str(role)+"**")
            elif role:
                failed_roles.append("**"+str(role)+"**")
        if not failed_roles:
            await interaction.response.send_message('Dla ' + uzytkownik.mention + ' usunięto role: ' + ', '.join(succesful_roles)) + "."
        else:
            message = "Dla " + uzytkownik.mention + " usunięto role: " + ', '.join(succesful_roles) + ".\nNie udało się usunąć(brak uprawnień lub błąd w przypadku warna): " + ', '.join(failed_roles) + "."
            await interaction.response.send_message(message)

    @usun_role.error
    async def usun_roleError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
           await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

    @app_commands.checks.has_any_role('Administracja', 'Moderacja')
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="cw", description="Cofa warny do uprzedniego stanu.")
    @app_commands.describe(uzytkownik="Osoba, której cofasz warna.")
    async def cofnij_warna(self, interaction: discord.Interaction, uzytkownik: discord.Member):
        async with self.bot.pool.acquire() as con:
            data_active = await con.fetch("SELECT * FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, True)
            if data_active:
                data_active = data_active[0]
                data_not_active = await con.fetch("SELECT * FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, False)
                if data_not_active:
                    data_not_active = data_not_active[0]
                    actual_warn_type = warns[data_active["typ"]]
                    new_warn_type = warns[data_not_active["typ"]]
                    actual_warn_role = get(interaction.guild.roles, name=actual_warn_type)
                    new_warn_role = get(interaction.guild.roles, name=new_warn_type)
                    channel = interaction.guild.get_channel(config.warn_channel_id)
                    message = channel.get_partial_message(data_active["message_id"])
                    embed = discord.Embed(title=str(new_warn_role), description=data_not_active["powod"] + " punkt regulaminu", colour=discord.Colour.red())
                    embed.add_field(name="Data otrzymania", value="<t:"+str(data_not_active["start"].timestamp())[:-2]+":F>")
                    embed.add_field(name="Data zakończenia", value="<t:"+str(data_not_active["koniec"].timestamp())[:-2]+":F>")
                    embed.add_field(name="Użytkownik", value=uzytkownik.mention, inline=False)
                    for autor in data_not_active["autorzy"]:
                        embed.add_field(name="Mod", value="<@"+str(autor)+">")
                    await message.edit(embed=embed)
                    await con.execute("DELETE FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, True)
                    await con.execute("UPDATE warn SET active=$1 WHERE id=$2;", True, uzytkownik.id)
                    await uzytkownik.remove_roles(actual_warn_role)
                    await uzytkownik.add_roles(new_warn_role)
                    await interaction.response.send_message(f"Cofnięto warna dla {uzytkownik.mention} z {actual_warn_role} do {new_warn_role}.")
                else:
                    warn_type = warns[data_active["typ"]]
                    warn_role = get(interaction.guild.roles, name=warn_type)
                    channel = interaction.guild.get_channel(config.warn_channel_id)
                    message = channel.get_partial_message(data_active["message_id"])
                    await message.delete()
                    await con.execute("DELETE FROM warn WHERE id=$1;", uzytkownik.id)
                    await uzytkownik.remove_roles(warn_role)
                    await interaction.response.send_message(f"Pomyślnie usunięto role warn dla {uzytkownik.mention}.")
            else:
                await interaction.response.send_message("Użytkownik nie ma warna.", ephemeral=True)


    @cofnij_warna.error
    async def cofnij_warnaError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
           await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Role(bot), guild = discord.Object(id = config.guild_id))
    