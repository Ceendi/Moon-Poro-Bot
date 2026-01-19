import datetime
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks
from discord.utils import get

import config
from config import warns, warn_days
from utils.constants import COLOR_ERROR, COLOR_EXPIRED
from utils.helpers import build_warn_embed, update_mod_stats
from utils.errors import handle_app_command_error

logger = logging.getLogger('discord.warn')


class WarnCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.czysc_warny.start()

    def cog_unload(self):
        self.czysc_warny.cancel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        data = await self.bot.pool.fetch("SELECT * FROM warn WHERE id=$1 AND active=$2;", member.id, True)
        if data:
            role = get(member.guild.roles, name=warns[data[0]["typ"]])
            if role and role not in member.roles:
                await member.add_roles(role)

    @tasks.loop(hours=1.0)
    async def czysc_warny(self):
        guild = self.bot.get_guild(config.guild_id)
        if not guild:
            return
            
        channel = guild.get_channel(config.warn_channel_id)
        
        async with self.bot.pool.acquire() as con:
            datas = await con.fetch("SELECT * FROM warn WHERE NOW() > koniec AND active=$1;", True)
            
            for data in datas:
                try:
                    await self._expire_warn(guild, channel, con, data)
                except Exception as e:
                    logger.error(f"Error expiring warn {data['id']}: {e}")

    async def _expire_warn(self, guild, channel, con, data):
        role = get(guild.roles, name=warns[data["typ"]])
        member = guild.get_member(data["id"])
        
        embed = build_warn_embed(
            title=str(role) if role else "Warn",
            powod=data['powod'],
            start=data['start'],
            koniec=data['koniec'],
            user_mention=f"<@{data['id']}>",
            autorzy=data["autorzy"],
            opis=data["opis"],
            colour=COLOR_EXPIRED
        )
        
        message = channel.get_partial_message(data["message_id"])
        try:
            await message.edit(embed=embed)
        except discord.NotFound:
            pass
        
        if member and role:
            await member.remove_roles(role)
            
        await con.execute("DELETE FROM warn WHERE id=$1;", data["id"])

    @czysc_warny.before_loop
    async def before_czysc_warny(self):
        await self.bot.wait_until_ready()

    @app_commands.checks.has_any_role("Moderacja", "Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="w", description="Warnuje użytkownika")
    @app_commands.describe(
        uzytkownik="Osoba, której dajesz warna",
        typ="Typ warna",
        powod="Powód warna (numer punktu regulaminu)",
        dodatkowy_powod="Opcjonalny dodatkowy powód",
        opis='Opcjonalny opis warna'
    )
    @app_commands.choices(typ=[
        Choice(name="Warn", value=1),
        Choice(name="Warn 2", value=2),
        Choice(name="TIMEOUT", value=3),
    ])
    async def warn(
        self,
        interaction: discord.Interaction,
        typ: int,
        uzytkownik: discord.Member,
        powod: app_commands.Range[int, 1, 13],
        dodatkowy_powod: Optional[app_commands.Range[int, 1, 13]] = None,
        opis: Optional[str] = None
    ):
        await interaction.response.defer()
        
        # Check if user already has TIMEOUT
        if any(role.name == "TIMEOUT" for role in uzytkownik.roles):
            await interaction.followup.send("Ten użytkownik ma już rolę **TIMEOUT**.", ephemeral=True)
            return

        powod_str = f"{powod}/{dodatkowy_powod}" if dodatkowy_powod else str(powod)
        data = await self.bot.pool.fetch('SELECT * FROM warn WHERE id=$1 AND active=$2;', uzytkownik.id, True)
        
        if data:
            await self._escalate_warn(interaction, uzytkownik, typ, powod_str, opis, data[0])
        else:
            await self._create_new_warn(interaction, uzytkownik, typ, powod_str, opis)

    async def _escalate_warn(self, interaction, uzytkownik, typ, powod_str, opis, existing_data):
        warn_channel = interaction.guild.get_channel(config.warn_channel_id)
        
        new_typ = min(existing_data["typ"] + typ, 3)
        old_warn_role = get(interaction.guild.roles, name=warns[existing_data["typ"]])
        warn_role = get(interaction.guild.roles, name=warns[new_typ])
        
        now = datetime.datetime.utcnow().replace(microsecond=0)
        end_date = now + datetime.timedelta(days=warn_days[str(warn_role)])
        
        new_powod = f"{existing_data['powod']}/{powod_str}"
        autorzy = list(existing_data["autorzy"])
        if interaction.user.id not in autorzy:
            autorzy.append(interaction.user.id)
        
        new_opis = f"{existing_data['opis']}\n{opis}" if existing_data["opis"] and opis else (opis or existing_data["opis"])
        
        embed = build_warn_embed(
            title=str(warn_role),
            powod=new_powod,
            start=existing_data["start"],
            koniec=end_date,
            user_mention=uzytkownik.mention,
            autorzy=autorzy,
            opis=new_opis,
            colour=COLOR_ERROR
        )
        
        message = warn_channel.get_partial_message(existing_data["message_id"])
        await message.edit(embed=embed)
        
        async with self.bot.pool.acquire() as con:
            await con.execute("DELETE FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, False)
            await con.execute('UPDATE warn SET active=$1 WHERE id=$2;', False, uzytkownik.id)
            await con.execute(
                "INSERT INTO warn VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9);",
                uzytkownik.id, new_typ, new_powod, existing_data["start"], end_date, message.id, autorzy, True, new_opis
            )
            await update_mod_stats(self.bot.pool, interaction.user.id, 'w')
        
        await uzytkownik.remove_roles(old_warn_role)
        await uzytkownik.add_roles(warn_role)
        await interaction.followup.send(f"{uzytkownik.mention} otrzymał **{warn_role}** za {powod_str} punkt regulaminu.")

    async def _create_new_warn(self, interaction, uzytkownik, typ, powod_str, opis):
        warn_channel = interaction.guild.get_channel(config.warn_channel_id)
        warn_role = get(interaction.guild.roles, name=warns[typ])
        
        now = datetime.datetime.utcnow().replace(microsecond=0)
        end_date = now + datetime.timedelta(days=warn_days[str(warn_role)])
        
        embed = build_warn_embed(
            title=str(warn_role),
            powod=powod_str,
            start=now,
            koniec=end_date,
            user_mention=uzytkownik.mention,
            autorzy=[interaction.user.id],
            opis=opis,
            colour=COLOR_ERROR
        )
        
        message = await warn_channel.send(embed=embed)
        
        async with self.bot.pool.acquire() as con:
            await con.execute(
                'INSERT INTO warn VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9);',
                uzytkownik.id, typ, powod_str, now, end_date, message.id, [interaction.user.id], True, opis
            )
            await update_mod_stats(self.bot.pool, interaction.user.id, 'w')
        
        await uzytkownik.add_roles(warn_role)
        await interaction.followup.send(f"{uzytkownik.mention} otrzymał **{warn_role}** za {powod_str} punkt regulaminu.")

    @app_commands.checks.has_any_role("Moderacja", "Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="cw", description="Cofa warna do poprzedniego stanu")
    @app_commands.describe(uzytkownik="Osoba, której cofasz warna")
    async def cofnij_warna(self, interaction: discord.Interaction, uzytkownik: discord.Member):
        async with self.bot.pool.acquire() as con:
            data_active = await con.fetch("SELECT * FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, True)
            if not data_active:
                await interaction.response.send_message("Użytkownik nie ma warna.", ephemeral=True)
                return
            
            data_active = data_active[0]
            data_previous = await con.fetch("SELECT * FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, False)
            
            if data_previous:
                await self._revert_to_previous_warn(interaction, uzytkownik, con, data_active, data_previous[0])
            else:
                await self._remove_warn_completely(interaction, uzytkownik, con, data_active)

    async def _revert_to_previous_warn(self, interaction, uzytkownik, con, current, previous):
        current_role = get(interaction.guild.roles, name=warns[current["typ"]])
        previous_role = get(interaction.guild.roles, name=warns[previous["typ"]])
        channel = interaction.guild.get_channel(config.warn_channel_id)
        
        embed = build_warn_embed(
            title=str(previous_role),
            powod=previous['powod'],
            start=previous['start'],
            koniec=previous['koniec'],
            user_mention=uzytkownik.mention,
            autorzy=previous["autorzy"],
            opis=previous["opis"],
            colour=COLOR_ERROR
        )
        
        message = channel.get_partial_message(current["message_id"])
        await message.edit(embed=embed)
        
        await con.execute("DELETE FROM warn WHERE id=$1 AND active=$2;", uzytkownik.id, True)
        await con.execute("UPDATE warn SET active=$1 WHERE id=$2;", True, uzytkownik.id)
        
        await uzytkownik.remove_roles(current_role)
        await uzytkownik.add_roles(previous_role)
        await interaction.response.send_message(f"Cofnięto warna dla {uzytkownik.mention} z **{current_role}** do **{previous_role}**.")

    async def _remove_warn_completely(self, interaction, uzytkownik, con, data):
        warn_role = get(interaction.guild.roles, name=warns[data["typ"]])
        channel = interaction.guild.get_channel(config.warn_channel_id)
        
        message = channel.get_partial_message(data["message_id"])
        try:
            await message.delete()
        except discord.NotFound:
            pass
        
        await con.execute("DELETE FROM warn WHERE id=$1;", uzytkownik.id)
        await uzytkownik.remove_roles(warn_role)
        await interaction.response.send_message(f"✅ Usunięto warna dla {uzytkownik.mention}.")

    @warn.error
    async def warn_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if not await handle_app_command_error(interaction, error):
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(WarnCog(bot), guild=discord.Object(id=config.guild_id))