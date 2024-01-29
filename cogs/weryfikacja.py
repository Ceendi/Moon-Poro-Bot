from pulsefire.clients import RiotAPIClient
import asyncio
from pulsefire.middlewares import rate_limiter_middleware
import discord
from discord.utils import get
from discord.ext import commands, tasks
from discord import app_commands
from discord.app_commands import Choice
from random import randrange
import config
from config import lol_ranks
from functions import has_rank_roles

client = RiotAPIClient(default_headers={"X-Riot-Token": config.riot_api_token})

class Zweryfikuj(discord.ui.View):
    def __init__(self, icon_id, nick, puuid, server, bot):
        super().__init__(timeout=120)
        self.icon_id = icon_id
        self.nick = nick
        self.puuid = puuid
        self.server = server
        self.bot = bot
    
    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.green)
    async def zweryfikuj(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        server_translation = {'EUN1': 'EUNE', 'EUW1': 'EUW', 'NA1': 'NA'}
        if "Zweryfikowany" in str(interaction.user.roles):
            await interaction.followup.send("Już jesteś zweryfikowany!", ephemeral=True)
            return
        async with client:
            summoner = await client.get_lol_summoner_v4_by_puuid(region=self.server, puuid=self.puuid)
            leagues = await client.get_lol_league_v4_entries_by_summoner(region=self.server, summoner_id=summoner["id"])
        if summoner['profileIconId'] == self.icon_id:
            if has_rank_roles(interaction.user):
                for role in interaction.user.roles:
                    if str(role) in lol_ranks:
                        await interaction.user.remove_roles(role)
            lol_rank = 'UNRANKED'
            for league in leagues:
                if league["queueType"] == 'RANKED_SOLO_5x5':
                    lol_rank = league["tier"]
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
            await self.bot.pool.execute("INSERT INTO zweryfikowani VALUES($1, $2, $3, $4);", interaction.user.id, message.id, summoner["id"], self.server)
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

    game_name = discord.ui.TextInput(label='Nick', required=True, placeholder='Twój nick..', min_length=3, max_length=16)
    tag = discord.ui.TextInput(label='TAG', required=True, placeholder='Twój tag..', min_length=3, max_length=6)
    server = discord.ui.TextInput(label='Server', required=True, default='EUNE', placeholder='Serwer twojego konta(EUNE, EUW, NA)..', min_length=2, max_length=4)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return (str(self.server).lower() in ['eune', 'euw', 'na'])

    async def on_submit(self, interaction: discord.Interaction):
        servers = {'eune': 'EUN1', 'euw': 'EUW1', 'na': 'NA1'}
        api_servers = {'eune': 'europe', 'euw': 'europe', 'na': 'americas'}
        self.server_translated = servers[str(self.server).lower()]
        self.api_server = api_servers[str(self.server).lower()]
        self.tag = str(self.tag).replace('#', '')
        async with client:
            summoner = await client.get_account_v1_by_riot_id(game_name=self.game_name, tag_line=self.tag, region=self.api_server)
            puuid = summoner["puuid"]
            summoner = await client.get_lol_summoner_v4_by_puuid(region=self.server_translated, puuid=puuid)
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE lol_id = $1;", summoner['id'])
        if not data:
            random_icon_id = randrange(0, 28)
            while summoner['profileIconId'] == random_icon_id:
                random_icon_id = randrange(0, 28)
            icon_url = f'https://raw.communitydragon.org/12.13/game/assets/ux/summonericons/profileicon{random_icon_id}.png'
            embed = discord.Embed(title='Weryfikacja', description=f'Na swoim koncie w lolu o nicku **{self.game_name}#{self.tag}** ustaw ikonkę, która pojawiła się niżej i gdy już to zrobisz, naciśnij na zielony przycisk na dole, żeby zweryfikować konto. Po 5 minutach przycisk przestanie działać!')
            embed.set_image(url=icon_url)
            view = Zweryfikuj(random_icon_id, str(self.game_name)+"#"+str(self.tag), puuid, self.server_translated, self.bot)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message("To konto w lolu już jest przypisane do innego użytkownika!", ephemeral=True)


    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if error.status == 404:
            await interaction.response.send_message(f"Nie znaleziono osoby o nicku **{self.game_name}#{self.tag}**!", ephemeral=True)
        else:
            await interaction.response.send_message("Coś poszło nie tak, spróbuj ponownie później!", ephemeral=True)
            raise error

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
            async with client:
                leagues = await client.get_lol_league_v4_entries_by_summoner(region=data[0]["server"], summoner_id=data[0]["lol_id"])
            for league in leagues:
                if league["queueType"] == 'RANKED_SOLO_5x5':
                    lol_rank = league["tier"]
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
        guild = self.bot.get_guild(config.guild_id)
        datas = await self.bot.pool.fetch("SELECT * FROM zweryfikowani;")
        datas = [data for data in datas if guild.get_member(data["id"])]

        async with client:
            for data in datas:
                member: discord.Member = guild.get_member(data["id"])
                old_user_roles = member.roles
                user_roles = member.roles
                
                if "Zweryfikowany" not in str(member.roles):
                    zweryfikowany = get(member.guild.roles, name="Zweryfikowany")
                    user_roles.append(zweryfikowany)
                
                if "Użytkownik" not in str(member.roles):
                    uzytkownik = get(member.guild.roles, name="Użytkownik")
                    user_roles.append(uzytkownik)

                for old_role in member.roles:
                    if str(old_role) in lol_ranks:
                        user_roles.remove(old_role)

                leagues = await client.get_lol_league_v4_entries_by_summoner(region=data["server"], summoner_id=data["lol_id"])

                lol_rank = 'UNRANKED'
                for league in leagues:
                    if league["queueType"] == 'RANKED_SOLO_5x5':
                        lol_rank = league["tier"]
                        break
                if lol_rank == "GRANDMASTER":
                    discord_new_rank = get(member.guild.roles, name='GrandMaster')
                else:
                    discord_new_rank = get(member.guild.roles, name=lol_rank.capitalize())

                user_roles.append(discord_new_rank)

                if old_user_roles != user_roles:
                    await member.edit(roles=user_roles)
                await asyncio.sleep(0.4)

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

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="usun_wer_nick", description="Usuwa dany nick z listy zweryfikowanych!")
    @app_commands.describe(
        nick = "Nick, jak nick to nazwa#123 to wpisujesz nazwa",
        tag = "Tag, jak nick to nazwa#123 to wpisujesz 123"
    )
    @app_commands.choices(
    server = [
        Choice(name = "EUNE", value = 'EUN1'),
        Choice(name = "EUW", value = 'EUW1'),
        Choice(name = "NA", value = 'NA1'),
    ]
    )
    async def usun_wer_nick(self, interaction: discord.Interaction, nick: str, tag: str, server: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        api_servers = {'EUN1': 'europe', 'EUW1': 'europe', 'NA1': 'americas'}
        tag = tag.replace('#', '')
        async with client:
            try:
                summoner = await client.get_account_v1_by_riot_id(game_name=nick, tag_line=tag, region=api_servers[server])
                puuid = summoner["puuid"]
                summoner = await client.get_lol_summoner_v4_by_puuid(region=server, puuid=puuid)
            except Exception as err:
                if err.status == 404:
                    await interaction.followup.send(f"Nie znaleziono osoby o nicku **{nick}#{tag}**!", ephemeral=True)
                    return
                else:
                    await interaction.followup.send(f"Wystapil blad, sprobuj ponownie pozniej!", ephemeral=True)
                    return
        data = await self.bot.pool.fetch("SELECT id, message_id FROM zweryfikowani WHERE lol_id=$1;", summoner["id"])
        if data:
            data = data[0]
            id = data[0]
            message_id = data[1]
            guild = self.bot.get_guild(config.guild_id)
            channel = guild.get_channel(config.zweryfikowani_channel_id)
            message = channel.get_partial_message(message_id)
            member = guild.get_member(id)
            if member:
                zweryfikowany = get(guild.roles, name="Zweryfikowany")
                if zweryfikowany in member.roles:
                    await member.remove_roles(zweryfikowany)
            await message.delete()
            await self.bot.pool.execute("DELETE FROM zweryfikowani WHERE lol_id=$1;", summoner['id'])
            await interaction.followup.send(f"Usunieto!", ephemeral=True)
        else:
            await interaction.followup.send(f"Ten nick nie jest na serwerze jako zweryfikowany.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(WeryfikacjaCog(bot), guild = discord.Object(id = config.guild_id))