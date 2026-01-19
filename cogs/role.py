from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get

import config
import functions
from utils.errors import handle_app_command_error
from utils.constants import ROLE_ZWERYFIKOWANY, ROLE_UZYTKOWNIK, ROLE_NPKWL


class RoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.checks.has_any_role("Administracja", "Moderacja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="dr", description="Dodaje rolę dla użytkownika")
    @app_commands.describe(uzytkownik="Osoba", role1="Rola", role2="Rola 2", role3="Rola 3", role4="Rola 4", role5="Rola 5")
    async def daj_role(self, interaction: discord.Interaction, uzytkownik: discord.Member, role1: discord.Role,
                       role2: Optional[discord.Role] = None, role3: Optional[discord.Role] = None,
                       role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None):
        roles = [r for r in [role1, role2, role3, role4, role5] if r]
        failed, success = [], []
        for role in roles:
            if role.name in config.ALLOWED_ROLES:
                if role not in uzytkownik.roles:
                    if role.name in config.lol_ranks and functions.has_rank_roles(uzytkownik):
                        for r in functions.get_member_rank_roles(uzytkownik):
                            await uzytkownik.remove_roles(r)
                            break
                    await uzytkownik.add_roles(role)
                    success.append(f"**{role}**")
            else:
                failed.append(f"**{role}**")
        msg = f"Dla {uzytkownik.mention} dodano role: {', '.join(success)}."
        if failed:
            msg += f"\n❌ Brak uprawnień do: {', '.join(failed)}."
        await interaction.response.send_message(msg)

    @app_commands.checks.has_any_role("Administracja", "Moderacja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="ur", description="Usuwa rolę użytkownikowi")
    @app_commands.describe(uzytkownik="Osoba", role1="Rola", role2="Rola 2", role3="Rola 3", role4="Rola 4", role5="Rola 5")
    async def usun_role(self, interaction: discord.Interaction, uzytkownik: discord.Member, role1: discord.Role,
                        role2: Optional[discord.Role] = None, role3: Optional[discord.Role] = None,
                        role4: Optional[discord.Role] = None, role5: Optional[discord.Role] = None):
        roles = [r for r in [role1, role2, role3, role4, role5] if r]
        failed, success = [], []
        for role in roles:
            if role.name in config.ALLOWED_ROLES:
                if role in uzytkownik.roles:
                    await uzytkownik.remove_roles(role)
                    success.append(f"**{role}**")
            else:
                failed.append(f"**{role}**")
        msg = f"Dla {uzytkownik.mention} usunięto role: {', '.join(success)}."
        if failed:
            msg += f"\n❌ Brak uprawnień do: {', '.join(failed)}."
        await interaction.response.send_message(msg)

    @app_commands.checks.has_any_role("Administracja", "Moderacja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="pbr", description="Pokazuje osoby ze zbugowanymi rolami")
    async def pokaz_zbugowanych(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uzytkownik_role = get(interaction.guild.roles, name=ROLE_UZYTKOWNIK)
        npkwl_role = get(interaction.guild.roles, name=ROLE_NPKWL)
        bugged = []
        for m in interaction.guild.members:
            is_bug = False
            if functions.has_rank_roles(m) and functions.has_server_roles(m) and uzytkownik_role not in m.roles:
                is_bug = True
            elif npkwl_role in m.roles and (functions.has_rank_roles(m) or functions.has_server_roles(m) or functions.has_other_roles(m)):
                is_bug = True
            elif npkwl_role in m.roles and uzytkownik_role not in m.roles:
                is_bug = True
            elif uzytkownik_role in m.roles and (not functions.has_rank_roles(m) or not functions.has_server_roles(m)) and npkwl_role not in m.roles:
                is_bug = True
            if len(functions.get_member_rank_roles(m)) > 1:
                is_bug = True
            if is_bug:
                bugged.append(m.mention)
            if len(bugged) > 30:
                await interaction.followup.send(', '.join(bugged))
                bugged = []
        if bugged:
            await interaction.followup.send(', '.join(bugged))
        await interaction.followup.send("✅ Skończono sprawdzanie ról.")

    @app_commands.checks.has_any_role("Administracja", "Moderacja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="nr", description="Naprawia zbugowane role")
    async def napraw_zbugowane(self, interaction: discord.Interaction):
        await interaction.response.defer()
        uzytkownik_role = get(interaction.guild.roles, name=ROLE_UZYTKOWNIK)
        npkwl_role = get(interaction.guild.roles, name=ROLE_NPKWL)
        fixed = 0
        for m in interaction.guild.members:
            if functions.has_rank_roles(m) and functions.has_server_roles(m) and uzytkownik_role not in m.roles:
                await m.add_roles(uzytkownik_role)
                fixed += 1
            elif npkwl_role in m.roles and uzytkownik_role not in m.roles:
                await m.add_roles(uzytkownik_role)
                fixed += 1
            elif uzytkownik_role in m.roles and (not functions.has_rank_roles(m) or not functions.has_server_roles(m)) and npkwl_role not in m.roles:
                await m.remove_roles(uzytkownik_role)
                fixed += 1
        await interaction.followup.send(f"✅ Naprawiono {fixed} użytkowników.")

    @app_commands.checks.has_any_role("Administracja", "Moderacja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="pr", description="Pokazuje niezweryfikowanych z daną rolą")
    @app_commands.describe(rola="Rola")
    async def pokaz_role(self, interaction: discord.Interaction, rola: discord.Role):
        await interaction.response.defer()
        members = []
        for m in interaction.guild.members:
            if rola in m.roles and ROLE_ZWERYFIKOWANY not in [r.name for r in m.roles]:
                members.append(m.mention)
            if len(members) > 30:
                await interaction.followup.send(', '.join(members))
                members = []
        if members:
            await interaction.followup.send(', '.join(members))
        await interaction.followup.send("✅ Skończono pokazywanie.")

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="wylacz_multikonta", description="Toggle sprawdzania multikont")
    async def wylacz_multikonta(self, interaction: discord.Interaction):
        self.bot.join_check = not self.bot.join_check
        status = "włączone" if self.bot.join_check else "wyłączone"
        await interaction.response.send_message(f"Sprawdzanie multikont: **{status}**", ephemeral=True)

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="napraw_weryfikacje", description="Naprawia nieprawidłowe weryfikacje")
    async def napraw_weryfikacje(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await self.bot.pool.fetch("SELECT id FROM zweryfikowani")
        verified_ids = {row['id'] for row in data}
        unranked_role = get(interaction.guild.roles, name="Unranked")
        fixed = 0
        for m in interaction.guild.members:
            if any(r.name == ROLE_ZWERYFIKOWANY for r in m.roles) and m.id not in verified_ids:
                new_roles = [r for r in m.roles if r.name != ROLE_ZWERYFIKOWANY and r.name not in config.lol_ranks]
                if unranked_role and unranked_role not in new_roles:
                    new_roles.append(unranked_role)
                await m.edit(roles=new_roles)
                fixed += 1
        await interaction.followup.send(f"✅ Naprawiono {fixed} użytkowników.")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if not await handle_app_command_error(interaction, error):
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleCog(bot), guild=discord.Object(id=config.guild_id))