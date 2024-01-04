import discord
from discord import app_commands
from discord.utils import get
from discord.ext import commands, tasks
from discord.app_commands import Choice
from typing import Optional
import config
from config import warns, warn_days
import datetime


class Warn(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.czysc_warny.start()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        data = await self.bot.pool.fetch("SELECT * FROM warn WHERE id=$1 AND active=$2;", member.id, True)
        if data:
            role = get(member.guild.roles, name=warns[data[0]["typ"]])
            if role not in member.roles:
                await member.add_roles(role)

    @tasks.loop(hours=1.0)
    async def czysc_warny(self):
        guild = self.bot.get_guild(config.guild_id)
        channel = guild.get_channel(config.warn_channel_id)
        print(guild)
        print(guild.id)
        print(guild.channels)
        print(channel.id)
        async with self.bot.pool.acquire() as con:
            datas = await con.fetch("SELECT * FROM warn WHERE NOW() > koniec AND active=$1;", True)
            for data in datas:
                role = get(guild.roles, name=warns[data["typ"]])
                uzytkownik = guild.get_member(data["id"])
                message = channel.get_partial_message(data["message_id"])
                embed = discord.Embed(title=str(role), description=data["powod"] + " punkt regulaminu", colour=discord.Colour.dark_gray())
                if data["opis"]:
                    embed.add_field(name="Opis", value=data["opis"])
                embed.add_field(name="Data otrzymania", value="<t:"+str(data["start"].timestamp())[:-2]+":F>")
                embed.add_field(name="Data zakończenia", value="<t:"+str(data["koniec"].timestamp())[:-2]+":F>")
                embed.add_field(name="Użytkownik", value="<@"+str(data["id"])+">", inline=False)
                for autor in data["autorzy"]:
                    embed.add_field(name="Mod", value="<@"+str(autor)+">", inline=True)
                await message.edit(embed=embed)
                if uzytkownik:
                    await uzytkownik.remove_roles(role)
                await con.execute("DELETE FROM warn WHERE id=$1;", data["id"])

    @czysc_warny.before_loop
    async def before_czysc_warny(self):
        await self.bot.wait_until_ready()

    @app_commands.checks.has_any_role("Moderacja", "Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="w", description="Warnuje użytkownika")
    @app_commands.describe(
        uzytkownik = "Osoba, której dajesz warna",
        typ = "Typ warna",
        powod = "Powód warna",
        dodatkowy_powod = "Opcjonalny powód warna",
        opis = 'Opcjonalny opis warna'
    )
    @app_commands.choices(
        typ = [
            Choice(name = "Warn", value = 1),
            Choice(name = "Warn 2", value = 2),
            Choice(name = "TIMEOUT", value = 3),
        ]
    )
    async def warn(self, interaction: discord.Interaction, typ: int, uzytkownik: discord.Member, powod: app_commands.Range[int, 1, 13], dodatkowy_powod: Optional[app_commands.Range[int, 1, 13]], opis: Optional[str]):
        if "TIMEOUT" in str(uzytkownik.roles):
            await interaction.response.send_message("Ten użytkownik ma już rolę **TIMEOUT**.", ephemeral=True)
            return

        warn_channel = interaction.guild.get_channel(config.warn_channel_id)
        data = await self.bot.pool.fetch('SELECT * FROM warn WHERE id=$1 AND active=$2;', uzytkownik.id, True)
        powod = str(powod)
        if dodatkowy_powod:
            powod = powod + '/' + str(dodatkowy_powod)
        if data:
            data = data[0]
            if data["typ"] + typ < 3:
                typ = data["typ"] + typ
                warn_type = warns[typ]
            else:
                typ = 3
                warn_type = "TIMEOUT"
            old_warn_role = get(interaction.guild.roles, name=warns[data["typ"]])
            warn_role = get(interaction.guild.roles, name=warn_type)
            now = datetime.datetime.utcnow().replace(microsecond=0)
            end_date = now + datetime.timedelta(days=warn_days[str(warn_role)])
            start_date = data["start"]
            new_powod = powod
            powod = data["powod"] + '/' + powod
            autorzy = data["autorzy"]
            if data["opis"] and opis:
                opis = data["opis"] + "\n" + opis
            elif data["opis"]:
                opis = data["opis"]
            embed = discord.Embed(title=str(warn_role), description=powod + " punkt regulaminu", colour=discord.Colour.red())
            if opis:
                embed.add_field(name="Opis", value=opis, inline=False)            
            embed.add_field(name="Data otrzymania", value="<t:"+str(start_date.timestamp())[:-2]+":F>")
            embed.add_field(name="Data zakończenia", value="<t:"+str(end_date.timestamp())[:-2]+":F>")
            embed.add_field(name="Użytkownik", value=uzytkownik.mention, inline=False)
            if interaction.user.id not in autorzy:
                autorzy.append(interaction.user.id)
            for autor in autorzy:
                embed.add_field(name="Mod", value="<@"+str(autor)+">")
            message = warn_channel.get_partial_message(data["message_id"])
            await message.edit(embed=embed)
            async with self.bot.pool.acquire() as con:
                await con.execute("DELETE FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, False)
                await con.execute('UPDATE warn SET active=$1 WHERE id=$2;', False, uzytkownik.id)
                await con.execute("INSERT INTO warn VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9);", uzytkownik.id, typ, powod, start_date, end_date, message.id, autorzy, True, opis)
            await uzytkownik.remove_roles(old_warn_role)
            await uzytkownik.add_roles(warn_role)
            await interaction.response.send_message(uzytkownik.mention + " otrzymał **" + str(warn_role) + "** za " + str(new_powod) + " punkt regulaminu.")
        else:
            warn_type = warns[typ]
            warn_role = get(interaction.guild.roles, name=warn_type)
            now = datetime.datetime.utcnow().replace(microsecond=0)
            end_date = now + datetime.timedelta(days=warn_days[str(warn_role)])
            embed = discord.Embed(title=str(warn_role), description=powod + " punkt regulaminu", colour=discord.Colour.red())
            if opis:
                embed.add_field(name="Opis", value=opis, inline=False)            
            embed.add_field(name="Data otrzymania", value="<t:"+str(now.timestamp())[:-2]+":F>")
            embed.add_field(name="Data zakończenia", value="<t:"+str(end_date.timestamp())[:-2]+":F>")
            embed.add_field(name="Użytkownik", value=uzytkownik.mention, inline=False)
            embed.add_field(name="Modzi", value=interaction.user.mention, inline=True)
            message = await warn_channel.send(embed=embed)
            await self.bot.pool.execute('INSERT INTO warn VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9);', uzytkownik.id, typ, powod, now, end_date, message.id, [interaction.user.id], True, opis)
            await uzytkownik.add_roles(warn_role)
            await interaction.response.send_message(uzytkownik.mention + " otrzymał **" + str(warn_role) + "** za " + str(powod) + " punkt regulaminu.")
        async with self.bot.pool.acquire() as con:
            mod_stat = await con.fetch('SELECT * FROM mod_stats WHERE id=$1;', interaction.user.id)
            if not mod_stat:
                await con.execute("INSERT INTO mod_stats(id) VALUES($1);", interaction.user.id)
            year = datetime.date.today().year%100
            month = datetime.date.today().strftime('%m')
            column_name = "wy" + str(year) + '_m' + str(month)
            await con.execute(f'''ALTER TABLE mod_stats ADD COLUMN IF NOT EXISTS zy{year}_m{month} SMALLINT DEFAULT 0;
                                    ALTER TABLE mod_stats ADD COLUMN IF NOT EXISTS wy{year}_m{month} SMALLINT DEFAULT 0;''')
            await con.execute(f"UPDATE mod_stats SET {column_name}={column_name}+1 WHERE id=$1;", interaction.user.id)

    @warn.error
    async def warnError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
           await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Warn(bot), guild = discord.Object(id = config.guild_id))