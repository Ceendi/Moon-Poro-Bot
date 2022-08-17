from pyot.conf.model import activate_model, ModelConf
from pyot.conf.pipeline import activate_pipeline, PipelineConf
from config import riot_api_token

@activate_model("lol")
class LolModel(ModelConf):
    default_platform = "eun1"
    default_region = "europe"
    default_version = "latest"
    default_locale = "en_us"


@activate_pipeline("lol")
class LolPipeline(PipelineConf):
    name = "lol_main"
    default = True
    stores = [
        {
            "backend": "pyot.stores.omnistone.Omnistone",
            "expirations": {
                "summoner_v4_by_name": 0,
                "league_v4_summoner_entries": 600,
            }
        },
        {
            "backend": "pyot.stores.riotapi.RiotAPI",
            "api_key": riot_api_token
        }
    ]

import discord
from discord.utils import get
from discord.ext import commands, tasks
from discord import app_commands
import config
import traceback
from random import randrange
from config import lol_ranks
from functions import has_rank_roles
from pyot.models import lol
from pyot.core.exceptions import NotFound


class Zweryfikuj(discord.ui.View):
    def __init__(self, icon_id, nick, server, bot):
        super().__init__(timeout=300)
        self.icon_id = icon_id
        self.nick = nick
        self.server = server
        self.bot = bot
    
    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.green)
    async def zweryfikuj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        server_translation = {'EUN1': 'EUNE', 'EUW1': 'EUW', 'NA1': 'NA'}
        if "Zweryfikowany" in str(interaction.user.roles):
            await interaction.followup.send("Już jesteś zweryfikowany!", ephemeral=True)
            return
        try:
            summoner = await lol.Summoner(platform=self.server, name=self.nick).get()
        except NotFound:
            await interaction.followup.send(f"Nie znaleziono osoby o nicku **{self.nick}**!", ephemeral=True)
            return
        if summoner.profile_icon_id == self.icon_id:
            if has_rank_roles(interaction.user):
                for role in interaction.user.roles:
                    if str(role) in lol_ranks:
                        await interaction.user.remove_roles(role)

            lol_rank = 'UNRANKED'
            leagues = await summoner.league_entries.get()
            for league in leagues:
                if league.queue == 'RANKED_SOLO_5x5':
                    lol_rank = league.tier
                    break
            if lol_rank == "GRANDMASTER":
                discord_new_rank = get(interaction.guild.roles, name='GrandMaster')
            else:
                discord_new_rank = get(interaction.guild.roles, name=lol_rank.capitalize())
            
            embed = discord.Embed(colour=discord.Colour.green())
            embed.add_field(name="Nick", value=interaction.user.mention)
            embed.add_field(name="Serwer", value=self.server)
            channel = interaction.guild.get_channel(config.zweryfikowani_channel_id)
            message = await channel.send(embed=embed)
            await self.bot.pool.execute("INSERT INTO zweryfikowani VALUES($1, $2, $3, $4);", interaction.user.id, message.id, summoner.id, self.server)
            zweryfikowany = get(interaction.guild.roles, name="Zweryfikowany")
            server_role = get(interaction.guild.roles, name=server_translation[self.server])
            uzytkownik_role = get(interaction.guild.roles, name="Użytkownik")
            await interaction.user.add_roles(*[discord_new_rank, server_role, uzytkownik_role, zweryfikowany])
            await interaction.followup.send("Udało Ci się przejść weryfikację!", ephemeral=True)
        else:
            await interaction.followup.send("Nie udało Ci się przejść weryfikacji, upewnij się, że nick oraz ikonka się zgadzają i spróbuj ponownie.", ephemeral=True)


class Weryfikacja(discord.ui.Modal, title="Weryfikacja"):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    nick = discord.ui.TextInput(label='Nick', required=True, placeholder='Twój nick..')
    server = discord.ui.TextInput(label='Server', required=True, default='EUNE', placeholder='Serwer twojego konta(EUNE, EUW, NA)..')

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(self.server).lower() in ['eune', 'euw', 'na']

    async def on_submit(self, interaction: discord.Interaction):
        servers = {'eune': 'EUN1', 'euw': 'EUW1', 'na': 'NA1'}
        self.server = servers[str(self.server).lower()]
        try:
            summoner = await lol.Summoner(platform=self.server, name=self.nick).get()
        except NotFound:
            await interaction.response.send_message(f"Nie znaleziono osoby o nicku **{self.nick}**!", ephemeral=True)
            return
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE lol_id = $1;", summoner.id)
        if not data:
            random_icon_id = randrange(0, 28)
            while summoner.profile_icon_id == random_icon_id:
                random_icon_id = randrange(0, 28)
            icon_url = f'https://raw.communitydragon.org/12.13/game/assets/ux/summonericons/profileicon{random_icon_id}.png'
            embed = discord.Embed(title='Weryfikacja', description=f'Na swoim koncie w lolu o nicku **{self.nick}** ustaw ikonkę, która pojawiła się niżej i gdy już to zrobisz, naciśnij na zielony przycisk na dole, żeby zweryfikować konto. Po 5 minutach przycisk przestanie działać!')
            embed.set_image(url=icon_url)
            view = Zweryfikuj(random_icon_id, self.nick, self.server, self.bot)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message("To konto w lolu już jest przypisane do innego użytkownika!", ephemeral=True)


    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message("Coś poszło nie tak, spróbuj ponownie!", ephemeral=True)
        traceback.print_tb(error.__traceback__)

class WeryfikacjaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sprawdz_zweryfikowanych.start()
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE id=$1;", member.id)
        server_translation = {'EUN1': 'EUNE', 'EUW1': 'EUW', 'NA1': 'NA'}
        if data:
            lol_rank = 'UNRANKED'
            leagues = await lol.SummonerLeague(summoner_id=data[0]["lol_id"], platform=data[0]["server"]).get()
            for league in leagues.entries:
                if league.queue == 'RANKED_SOLO_5x5':
                    lol_rank = league.tier
                    break
            if lol_rank == "GRANDMASTER":
                discord_new_rank = get(member.guild.roles, name='GrandMaster')
            else:
                discord_new_rank = get(member.guild.roles, name=lol_rank.capitalize())

            zweryfikowany = get(member.guild.roles, name='Zweryfikowany')
            uzytkownik = get(member.guild.roles, name='Użytkownik')
            server = get(member.guild.roles, name=server_translation[data[0]['server']])
            await member.add_roles(*[server, uzytkownik, zweryfikowany, discord_new_rank])
            try:
                await member.send("Byłeś zweryfikowany, więc bot automatycznie przyznał Ci role, jeśli chcesz usunąć weryfikację użyj komendy /usun_weryfikacje.")
            except discord.errors.Forbidden:
                pass

    @tasks.loop(hours=24.0)
    async def sprawdz_zweryfikowanych(self):
        datas = await self.bot.pool.fetch("SELECT * FROM zweryfikowani;")
        for data in datas:
            guild = self.bot.get_guild(config.guild_id)
            member = guild.get_member(data['id'])
            if member:
                for old_role in member.roles:
                    if str(old_role) in lol_ranks:
                        break
            
                lol_rank = 'UNRANKED'
                leagues = await lol.SummonerLeague(summoner_id=data["lol_id"], platform=data["server"]).get()
                for league in leagues.entries:
                    if league.queue == 'RANKED_SOLO_5x5':
                        lol_rank = league.tier
                        break
                if lol_rank == "GRANDMASTER":
                    discord_new_rank = get(member.guild.roles, name='GrandMaster')
                else:
                    discord_new_rank = get(member.guild.roles, name=lol_rank.capitalize())

                if discord_new_rank != old_role:
                    await member.remove_roles(old_role)
                    await member.add_roles(discord_new_rank)

    @sprawdz_zweryfikowanych.before_loop
    async def beofre_sprawdz_zweryfikowanych(self):
        await self.bot.wait_until_ready()

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="usun_weryfikacje", description="Usuwa ciebie z listy zweryfikowanych, pamiętaj że twoja rola już nie będzie odświeżana!")
    async def usun_weryfikacje(self, interaction: discord.Interaction):
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE id=$1;", interaction.user.id)
        if not data:
            await interaction.response.send_message("Nie jesteś na liście zweryfikowanych!", ephemeral=True)
            return
        zweryfikowany = get(interaction.guild.roles, name="Zweryfikowany")
        channel = interaction.guild.get_channel(config.zweryfikowani_channel_id)
        message = channel.get_partial_message(data[0]['message_id'])
        await message.delete()
        await self.bot.pool.execute("DELETE FROM zweryfikowani WHERE id=$1;", interaction.user.id)
        await interaction.user.remove_roles(zweryfikowany)
        await interaction.response.send_message("Pomyślnie usunięto Cię z listy zweryfikowanych!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeryfikacjaCog(bot), guild = discord.Object(id = config.guild_id))