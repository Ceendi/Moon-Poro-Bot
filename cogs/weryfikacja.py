import discord
from discord.utils import get
from discord.ext import commands, tasks
from discord import app_commands
import config
import traceback
from riotwatcher import LolWatcher, ApiError
from random import randrange
from config import lol_ranks
from functions import has_rank_roles


lol_watcher = LolWatcher(config.riot_api_token)


class Zweryfikuj(discord.ui.View):
    def __init__(self, icon_id, nick, server, bot):
        super().__init__(timeout=300)
        self.icon_id = icon_id
        self.nick = nick
        self.server = server
        self.bot = bot
    
    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.green)
    async def zweryfikuj(self, interaction: discord.Interaction, button: discord.ui.Button):
        if "Zweryfikowany" in interaction.user.roles:
            await interaction.response.send_message("Już jesteś zweryfikowany!", ephemeral=True)
            return
        try:
            summoner = lol_watcher.summoner.by_name(self.server, self.nick)
        except ApiError as error:
            if error.response.status_code == 429:
                await interaction.response.send_message(f"Spróbuj ponownie za {error.headers['Retry-After']}")
                return
            elif error.response.status_code == 404:
                await interaction.response.send_message(f"Nie znaleziono osoby o nicku **{self.nick}**!", ephemeral=True)
                return
            else:
                raise error
        if summoner['profileIconId'] == self.icon_id:
            if has_rank_roles(interaction.user):
                for role in interaction.user.roles:
                    if str(role) in lol_ranks:
                        await interaction.user.remove_roles(role)

            lol_rank = 'UNRANKED'
            leagues = lol_watcher.league.by_summoner(self.server, summoner['id'])
            if leagues:
                for league in leagues:
                    if league['queueType'] == 'RANKED_SOLO_5x5':
                        lol_rank = league['tier']
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
            await self.bot.pool.execute("INSERT INTO zweryfikowani VALUES($1, $2, $3, $4);", interaction.user.id, message.id, summoner['id'], self.server)
            zweryfikowany = get(interaction.guild.roles, name="Zweryfikowany")
            await interaction.user.add_roles(discord_new_rank)
            await interaction.user.add_roles(zweryfikowany)
            await interaction.response.send_message("Udało ci się przejść weryfikację!", ephemeral=True)
        else:
            await interaction.response.send_message("Nie udało ci się przejść weryfikacji, upewnij się, że nick oraz ikonka się zgadzają i spróbuj ponownie.", ephemeral=True)


class Weryfikacja(discord.ui.Modal, title="Weryfikacja"):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    nick = discord.ui.TextInput(label='Nick na lolu', required=True)
    server = discord.ui.Select(
        max_values=1,
        placeholder='Wybierz region konta...',
        options = [
            discord.SelectOption(label='EUNE', value="EUN1"),
            discord.SelectOption(label='EUW', value="EUW1"),
            discord.SelectOption(label='NA', value='NA1')
        ]
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            icon = lol_watcher.summoner.by_name(self.server.values[0], self.nick)['profileIconId']
        except ApiError as error:
            if error.response.status_code == 429:
                await interaction.response.send_message(f"Spróbuj ponownie za {error.headers['Retry-After']}")
                return
            elif error.response.status_code == 404:
                await interaction.response.send_message(f"Nie znaleziono osoby o nicku **{self.nick}**!", ephemeral=True)
                return
            else:
                raise error
        random_icon_id = randrange(0, 28)
        while icon == random_icon_id:
            random_icon_id = randrange(0, 28)
        icon_url = f'https://raw.communitydragon.org/12.13/game/assets/ux/summonericons/profileicon{random_icon_id}.png'
        embed = discord.Embed(title='Weryfikacja', description=f'Na swoim koncie w lolu o nicku **{self.nick}** ustaw ikonkę, która pojawiła się niżej i gdy już to zrobisz naciśnij na zielony przycisk na dole, żeby zweryfikować konto. Po 5 minutach przycisk przestanie działać!')
        embed.set_image(url=icon_url)
        view = Zweryfikuj(random_icon_id, self.nick, self.server.values[0], self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


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
        if data:
            lol_rank = 'UNRANKED'
            leagues = lol_watcher.league.by_summoner(data[0]['server'], data[0]['lol_id'])
            if leagues:
                for league in leagues:
                    if league['queueType'] == 'RANKED_SOLO_5x5':
                        lol_rank = league['tier']
                        break
            if lol_rank == "GRANDMASTER":
                rank_role = get(member.guild.roles, name='GrandMaster')
            else:
                rank_role = get(member.guild.roles, name=lol_rank.capitalize())

            role = get(member.guild.roles, name='Zweryfikowany')
            await member.add_roles(role)
            await member.add_roles(rank_role)

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
                leagues = lol_watcher.league.by_summoner(data['server'], data['lol_id'])
                if leagues:
                    for league in leagues:
                        if league['queueType'] == 'RANKED_SOLO_5x5':
                            lol_rank = league['tier']
                            break
                if lol_rank == "GRANDMASTER":
                    new_role = get(member.guild.roles, name='GrandMaster')
                else:
                    new_role = get(member.guild.roles, name=lol_rank.capitalize())

                if new_role != old_role:
                    await member.remove_roles(old_role)
                    await member.add_roles(new_role)

    @sprawdz_zweryfikowanych.before_loop
    async def beofre_sprawdz_zweryfikowanych(self):
        await self.bot.wait_until_ready()

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="usun_weryfikacje", description="Usuwa ciebie z listy zweryfikowanych, pamiętaj że twoja rola już nie będzie odświeżana!")
    async def usun_weryfikacje(self, interaction: discord.Interaction):
        data = await self.bot.pool.fetch("SELECT * FROM zweryfikowani WHERE id=$1;", interaction.user.id)
        if not data:
            await interaction.response.send_message("Nie jesteś na liście zweryfikowanych!", ephemeral=True)
        zweryfikowany = get(interaction.guild.roles, name="Zweryfikowany")
        channel = interaction.guild.get_channel(config.zweryfikowani_channel_id)
        message = channel.get_partial_message(data[0]['message_id'])
        await message.delete()
        await self.bot.pool.execute("DELETE FROM zweryfikowani WHERE id=$1;", interaction.user.id)
        await interaction.user.remove_roles(zweryfikowany)
        await interaction.response.send_message("Pomyślnie usunięto ciebie z listy zweryfikowanych!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeryfikacjaCog(bot), guild = discord.Object(id = config.guild_id))