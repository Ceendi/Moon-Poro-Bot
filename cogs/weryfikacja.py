import asyncio
import logging
from random import randrange

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks
from discord.utils import get

import config
from functions import has_rank_roles, get_member_rank_roles
from utils.constants import (
    SERVER_TRANSLATION, SERVERS, API_SERVERS,
    ROLE_ZWERYFIKOWANY, ROLE_UZYTKOWNIK,
    get_profile_icon_url
)
from utils.riot import get_rank_from_leagues, get_discord_rank_role
from utils.helpers import has_role, safe_api_call
from utils.errors import safe_send

logger = logging.getLogger('discord.weryfikacja')


class ZweryfikujView(discord.ui.View):
    def __init__(self, icon_id: int, nick: str, puuid: str, server: str, bot: commands.Bot):
        super().__init__(timeout=config.VERIFICATION_TIMEOUT)
        self.icon_id = icon_id
        self.nick = nick
        self.puuid = puuid
        self.server = server
        self.bot = bot
        self.message: discord.Message = None
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                embed = discord.Embed(
                    title="⏰ Czas minął!",
                    description="Weryfikacja wygasła. Kliknij przycisk ponownie, aby rozpocząć od nowa.",
                    colour=discord.Colour.orange()
                )
                await self.message.edit(embed=embed, view=self)
            except discord.NotFound:
                pass
    
    @discord.ui.button(label="✓ Zweryfikuj", style=discord.ButtonStyle.green)
    async def zweryfikuj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if has_role(interaction.user, ROLE_ZWERYFIKOWANY):
            await interaction.followup.send("Już jesteś zweryfikowany!", ephemeral=True)
            return
        
        summoner = await safe_api_call(
            self.bot.riot_client.get_lol_summoner_v4_by_puuid(region=self.server, puuid=self.puuid)
        )
        if not summoner:
            await interaction.followup.send("❌ Wystąpił błąd podczas komunikacji z API Riot. Spróbuj ponownie.", ephemeral=True)
            return
        
        if summoner['profileIconId'] != self.icon_id:
            await interaction.followup.send("❌ Nie udało się przejść weryfikacji. Upewnij się, że ikonka się zgadza.", ephemeral=True)
            return
        
        leagues = await safe_api_call(
            self.bot.riot_client.get_lol_league_v4_entries_by_puuid(region=self.server, puuid=self.puuid),
            default=[]
        )
        
        for role in get_member_rank_roles(interaction.user):
            await interaction.user.remove_roles(role)
        
        rank = get_rank_from_leagues(leagues)
        discord_rank = get_discord_rank_role(interaction.guild, rank)
        
        zweryfikowany = get(interaction.guild.roles, name=ROLE_ZWERYFIKOWANY)
        server_role = get(interaction.guild.roles, name=SERVER_TRANSLATION[self.server])
        uzytkownik_role = get(interaction.guild.roles, name=ROLE_UZYTKOWNIK)
        
        channel = interaction.guild.get_channel(config.zweryfikowani_channel_id)
        embed = discord.Embed(colour=discord.Colour.green())
        embed.add_field(name="Nick", value=interaction.user.mention)
        embed.add_field(name="Serwer", value=SERVER_TRANSLATION[self.server])
        message = await channel.send(embed=embed)
        
        await self.bot.pool.execute(
            "INSERT INTO zweryfikowani(id, message_id, server, puuid) VALUES($1, $2, $3, $4);",
            interaction.user.id, message.id, self.server, summoner["puuid"]
        )
        
        roles_to_add = [r for r in [discord_rank, server_role, uzytkownik_role, zweryfikowany] if r]
        await interaction.user.add_roles(*roles_to_add)
        
        self.stop()
        await interaction.followup.send("✅ Udało Ci się przejść weryfikację!", ephemeral=True)


class WeryfikacjaModal(discord.ui.Modal, title="Weryfikacja"):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=config.VERIFICATION_TIMEOUT)
        self.bot = bot

    game_name = discord.ui.TextInput(label='Nick', required=True, placeholder='Twój nick..', min_length=3, max_length=16)
    tag = discord.ui.TextInput(label='TAG', required=True, placeholder='Twój tag..', min_length=3, max_length=6)
    server = discord.ui.TextInput(label='Server', required=True, default='EUNE', placeholder='EUNE, EUW lub NA', min_length=2, max_length=4)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        server_lower = str(self.server.value).lower()
        if server_lower not in SERVERS:
            await interaction.response.send_message("❌ Nieprawidłowy serwer! Dostępne: EUNE, EUW, NA", ephemeral=True)
            return False
        return True

    async def on_submit(self, interaction: discord.Interaction):
        server_lower = str(self.server.value).lower()
        server_code = SERVERS[server_lower]
        api_region = API_SERVERS[server_lower]
        tag_clean = str(self.tag.value).replace('#', '')
        
        account = await safe_api_call(
            self.bot.riot_client.get_account_v1_by_riot_id(game_name=self.game_name.value, tag_line=tag_clean, region=api_region)
        )
        if not account:
            await interaction.response.send_message(f"❌ Nie znaleziono osoby o nicku **{self.game_name.value}#{tag_clean}**!", ephemeral=True)
            return
        
        summoner = await safe_api_call(
            self.bot.riot_client.get_lol_summoner_v4_by_puuid(region=server_code, puuid=account["puuid"])
        )
        if not summoner:
            await interaction.response.send_message("❌ Wystąpił błąd podczas komunikacji z API Riot.", ephemeral=True)
            return
        
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE puuid = $1;", summoner['puuid'])
        if data:
            await interaction.response.send_message("❌ To konto w LoL jest już przypisane do innego użytkownika!", ephemeral=True)
            return
        
        random_icon_id = randrange(0, 28)
        while summoner['profileIconId'] == random_icon_id:
            random_icon_id = randrange(0, 28)
        
        embed = discord.Embed(
            title='🔐 Weryfikacja',
            description=f'Na swoim koncie **{self.game_name.value}#{tag_clean}** ustaw ikonkę widoczną poniżej, a następnie kliknij zielony przycisk.\n\n⏰ Masz {config.VERIFICATION_TIMEOUT // 60} minuty!'
        )
        embed.set_image(url=get_profile_icon_url(random_icon_id))
        
        view = ZweryfikujView(random_icon_id, f"{self.game_name.value}#{tag_clean}", summoner["puuid"], server_code, self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error(f"Modal error: {error}")
        await safe_send(interaction, "❌ Wystąpił nieoczekiwany błąd. Spróbuj ponownie później.")


class WeryfikacjaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sprawdz_zweryfikowanych.start()

    def cog_unload(self):
        self.sprawdz_zweryfikowanych.cancel()

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        if entry.action != discord.AuditLogAction.member_role_update:
            return
        if entry.target != entry.user:
            return
        if not has_role(entry.target, ROLE_ZWERYFIKOWANY):
            return
        if len(entry.after.roles) == 1:
            added_role = entry.after.roles[0]
            if added_role.name in config.lol_ranks:
                roles = list(entry.target.roles)
                roles.remove(added_role)
                if len(entry.before.roles) == 1:
                    roles.append(entry.before.roles[0])
                await entry.user.edit(roles=roles)
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE id=$1;", member.id)
        if not data or not data[0]["puuid"]:
            return
        
        leagues = await safe_api_call(
            self.bot.riot_client.get_lol_league_v4_entries_by_puuid(region=data[0]["server"], puuid=data[0]["puuid"]),
            default=[]
        )
        
        rank = get_rank_from_leagues(leagues)
        discord_rank = get_discord_rank_role(member.guild, rank)
        
        zweryfikowany = get(member.guild.roles, name=ROLE_ZWERYFIKOWANY)
        uzytkownik = get(member.guild.roles, name=ROLE_UZYTKOWNIK)
        server = get(member.guild.roles, name=SERVER_TRANSLATION[data[0]['server']])
        
        roles_to_add = [r for r in [server, uzytkownik, zweryfikowany, discord_rank] if r]
        await member.add_roles(*roles_to_add)
        
        try:
            await member.send("Byłeś zweryfikowany, więc bot automatycznie przyznał Ci role. Jeśli chcesz usunąć weryfikację, użyj komendy `/usun_weryfikacje`.")
        except discord.errors.Forbidden:
            pass

    @tasks.loop(hours=24.0)
    async def sprawdz_zweryfikowanych(self):
        guild = self.bot.get_guild(config.guild_id)
        if not guild:
            return
            
        datas = await self.bot.pool.fetch("SELECT * FROM zweryfikowani;")
        
        for data in datas:
            member = guild.get_member(data["id"])
            if not member or not data["puuid"]:
                continue
            
            leagues = await safe_api_call(
                self.bot.riot_client.get_lol_league_v4_entries_by_puuid(region=data["server"], puuid=data["puuid"]),
                default=None
            )
            if leagues is None:
                logger.warning(f"Failed to fetch leagues for {data['id']}")
                continue
            
            current_roles = list(member.roles)
            
            zweryfikowany = get(guild.roles, name=ROLE_ZWERYFIKOWANY)
            uzytkownik = get(guild.roles, name=ROLE_UZYTKOWNIK)
            
            if zweryfikowany and zweryfikowany not in current_roles:
                current_roles.append(zweryfikowany)
            if uzytkownik and uzytkownik not in current_roles:
                current_roles.append(uzytkownik)
            
            current_roles = [r for r in current_roles if r.name not in config.lol_ranks]
            
            rank = get_rank_from_leagues(leagues)
            discord_rank = get_discord_rank_role(guild, rank)
            if discord_rank:
                current_roles.append(discord_rank)
            
            if set(current_roles) != set(member.roles):
                await member.edit(roles=current_roles)
            
            await asyncio.sleep(0.5)

    @sprawdz_zweryfikowanych.before_loop
    async def before_sprawdz_zweryfikowanych(self):
        await self.bot.wait_until_ready()

    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="usun_weryfikacje", description="Usuwa ciebie z listy zweryfikowanych")
    async def usun_weryfikacje(self, interaction: discord.Interaction):
        zweryfikowany = get(interaction.guild.roles, name=ROLE_ZWERYFIKOWANY)
        channel = interaction.guild.get_channel(config.zweryfikowani_channel_id)
        
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE id=$1;", interaction.user.id)
        
        if data:
            message = channel.get_partial_message(data[0]['message_id'])
            try:
                await message.delete()
            except discord.NotFound:
                pass
            await self.bot.pool.execute("DELETE FROM zweryfikowani WHERE id=$1;", interaction.user.id)
        
        if zweryfikowany:
            await interaction.user.remove_roles(zweryfikowany)
        
        await interaction.response.send_message("✅ Pomyślnie usunięto Cię z listy zweryfikowanych!", ephemeral=True)

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="usun_wer_nick", description="Usuwa dany nick z listy zweryfikowanych")
    @app_commands.describe(nick="Nick w grze (bez tagu)", tag="Tag (np. EUW)", server="Serwer konta")
    @app_commands.choices(server=[Choice(name="EUNE", value='EUN1'), Choice(name="EUW", value='EUW1'), Choice(name="NA", value='NA1')])
    async def usun_wer_nick(self, interaction: discord.Interaction, nick: str, tag: str, server: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        tag_clean = tag.replace('#', '')
        account = await safe_api_call(
            self.bot.riot_client.get_account_v1_by_riot_id(game_name=nick, tag_line=tag_clean, region=API_SERVERS[server])
        )
        if not account:
            await interaction.followup.send(f"❌ Nie znaleziono osoby o nicku **{nick}#{tag_clean}**!", ephemeral=True)
            return
        
        data = await self.bot.pool.fetch("SELECT id, message_id FROM zweryfikowani WHERE puuid=$1;", account["puuid"])
        if not data:
            await interaction.followup.send("❌ Ten nick nie jest przypisany jako zweryfikowany.", ephemeral=True)
            return
        
        guild = self.bot.get_guild(config.guild_id)
        channel = guild.get_channel(config.zweryfikowani_channel_id)
        
        member = guild.get_member(data[0]['id'])
        if member:
            zweryfikowany = get(guild.roles, name=ROLE_ZWERYFIKOWANY)
            if zweryfikowany and zweryfikowany in member.roles:
                await member.remove_roles(zweryfikowany)
        
        try:
            await channel.get_partial_message(data[0]['message_id']).delete()
        except discord.NotFound:
            pass
        
        await self.bot.pool.execute("DELETE FROM zweryfikowani WHERE puuid=$1;", account["puuid"])
        await interaction.followup.send("✅ Usunięto!", ephemeral=True)

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="show_wer_user", description="Pokazuje Discord usera na podstawie nicku z LoL")
    @app_commands.describe(nick="Nick z LoL", tag="Tag z LoL", server="Serwer konta")
    @app_commands.choices(server=[Choice(name="EUNE", value='EUN1'), Choice(name="EUW", value='EUW1'), Choice(name="NA", value='NA1')])
    async def show_wer_user(self, interaction: discord.Interaction, nick: str, tag: str, server: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        tag_clean = tag.replace('#', '')
        account = await safe_api_call(
            self.bot.riot_client.get_account_v1_by_riot_id(game_name=nick, tag_line=tag_clean, region=API_SERVERS[server])
        )
        if not account:
            await interaction.followup.send(f"❌ Nie znaleziono osoby o nicku **{nick}#{tag_clean}**!", ephemeral=True)
            return
        
        data = await self.bot.pool.fetch("SELECT id FROM zweryfikowani WHERE puuid=$1;", account["puuid"])
        
        if data:
            await interaction.followup.send(f"Użytkownik **{nick}#{tag_clean}** to <@{data[0]['id']}> (ID: {data[0]['id']})", ephemeral=True)
        else:
            await interaction.followup.send("❌ Ten nick nie jest przypisany do żadnego zweryfikowanego użytkownika.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeryfikacjaCog(bot), guild=discord.Object(id=config.guild_id))
