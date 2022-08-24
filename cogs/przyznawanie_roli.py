from os import remove
import discord
from discord import app_commands
from discord.ext import commands
import config
from discord.utils import get
from functions import has_rank_roles, has_server_roles, has_other_roles
from cogs.weryfikacja import Weryfikacja

class ButtonOnCooldown(commands.CommandError):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after

def key(interaction: discord.Interaction):
    return interaction.user

async def give_other_roles(interaction: discord.Interaction, button: discord.ui.Button):
    role = get(interaction.guild.roles, name=button.label)
    if role in interaction.user.roles:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(f"Usunąłeś rolę **{str(role)}**.", ephemeral=True)
        if str(role) == "Nie posiadam konta w lolu":
            uzytkownik = get(interaction.guild.roles, name="Użytkownik")
            await interaction.user.remove_roles(uzytkownik)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Dodałeś rolę **{str(role)}**.", ephemeral=True)
        if str(role) == "Nie posiadam konta w lolu":
            uzytkownik = get(interaction.guild.roles, name="Użytkownik")
            await interaction.user.add_roles(uzytkownik)


async def give_league_roles(interaction: discord.Interaction, button: discord.ui.Button, bot):
    if "Nie posiadam konta w lolu" in str(interaction.user.roles):
        await interaction.response.send_message(f"Nie możesz dostać roli ligowej posiadając rolę **Nie posiadam konta w lolu**.", ephemeral=True)
        return
    if "Zweryfikowany" in str(interaction.user.roles) and button.label in ["EUNE", "EUW", "NA"]:
        server_translation = {'EUN1': 'EUNE', 'EUW1': 'EUW', 'NA1': 'NA'}
        server = await bot.pool.fetch('SELECT server FROM zweryfikowani WHERE id=$1;', interaction.user.id)
        server = server_translation[server[0][0]]
        if server == button.label:
            await interaction.response.send_message("Masz zweryfikowane konto na tym regionie, nie możesz usunąć tej roli.", ephemeral=True)
            return
    role = get(interaction.guild.roles, name=button.label)
    if role in interaction.user.roles:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(f"Usunąłeś rolę **{str(role)}**.", ephemeral=True)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Dodałeś rolę **{str(role)}**.", ephemeral=True)

    await give_uzytkownik(interaction)


async def give_rank_role(interaction: discord.Interaction, select: discord.ui.Select):
    if "Nie posiadam konta w lolu" in str(interaction.user.roles):
        await interaction.response.send_message(f"Nie możesz dostać roli ligowej posiadając rolę **Nie posiadam konta w lolu.**", ephemeral=True)
        return
    role = get(interaction.guild.roles, name=select.values[0])
    previous_rank_role = None
    for r in interaction.user.roles:
        if str(r) in config.lol_ranks:
            previous_rank_role = get(interaction.guild.roles, name=str(r))
            break
    if previous_rank_role == role:
        await interaction.response.send_message(f"Już posiadasz rolę **{role}**.", ephemeral=True)
        return

    if previous_rank_role:
        await interaction.user.remove_roles(previous_rank_role)
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Zmieniłeś rolę **{str(previous_rank_role)}** na **{str(role)}**.", ephemeral=True)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Wybrałeś rolę **{str(role)}**.", ephemeral=True)

    await give_uzytkownik(interaction)


async def give_uzytkownik(interaction: discord.Interaction):
    uzytkownik = get(interaction.guild.roles, name="Użytkownik")
    user = interaction.guild.get_member(interaction.user.id) #need to refresh user to get actual roles

    if has_server_roles(user) and has_rank_roles(user):
        if uzytkownik not in user.roles:
            await user.add_roles(uzytkownik)
    elif uzytkownik in user.roles:
        await user.remove_roles(uzytkownik)


class Rangowe(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        max_values=1,
        placeholder="Aktualna dywizja solo/duo (najwyższa)",
        custom_id="ranks",
        row=0,
        options=[
            discord.SelectOption(
                label="Unranked",
                emoji='<:unranked:930537559988264960>',
            ),
            discord.SelectOption(
                label="Iron",
                emoji='<:iron:930539302851579934>',
            ),
            discord.SelectOption(
                label="Bronze",
                emoji='<:bronze:930537590552158249>',
            ),
            discord.SelectOption(
                label="Silver",
                emoji='<:silver:930537622131073114>',
            ),
            discord.SelectOption(
                label="Gold",
                emoji='<:gold:930537644222464011>',
            ),
            discord.SelectOption(
                label="Platinum",
                emoji='<:platinum:930537679265869844>',
            ),
            discord.SelectOption(
                label="Diamond",
                emoji='<:diamond:930537731573026877>',
            ),
            discord.SelectOption(
                label="Master",
                emoji='<:master:930537748736139294>',
            ),
            discord.SelectOption(
                label="GrandMaster",
                emoji='<:grandmaster:930537757808398366>',
            ),
            discord.SelectOption(
                label="Challenger",
                emoji='<:challenger:930537769699262525>',
            ),
        ]
    )
    async def ranks(self, interaction: discord.Interaction, select: discord.ui.Select):
        if "Zweryfikowany" not in str(interaction.user.roles):
            await give_rank_role(interaction, select)
        else:
            await interaction.response.send_message("Posiadasz rolę **Zweryfikowany**, która automatycznie aktualizuje Ci rolę co 24h! Jeśli chcesz zmienić konto to użyj komendy /usun_weryfikacje.", ephemeral=True)

class Serwerowe(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="EUNE", style=discord.ButtonStyle.red, custom_id="eune", row=0)
    async def eune(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="EUW", style=discord.ButtonStyle.red, custom_id="euw", row=0)
    async def euw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="NA", style=discord.ButtonStyle.red, custom_id="na", row=0)
    async def na(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

class Opcjonalne(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Top", style=discord.ButtonStyle.green, custom_id="top", row=0)
    async def top(self, interaction: discord.Interaction, button: discord.ui.Button):
       await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="Jungle", style=discord.ButtonStyle.green, custom_id="jungle", row=0)
    async def jungle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="Mid", style=discord.ButtonStyle.green, custom_id="mid", row=0)
    async def mid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="ADC", style=discord.ButtonStyle.green, custom_id="adc", row=0)
    async def adc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="Support", style=discord.ButtonStyle.green, custom_id="support", row=0)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button, self.bot)

    @discord.ui.button(label="TFT", style=discord.ButtonStyle.blurple, custom_id="tft", row=1)
    async def tft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="LOR", style=discord.ButtonStyle.blurple, custom_id="lor", row=1)
    async def lor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="Valorant", style=discord.ButtonStyle.blurple, custom_id="valorant", row=1)
    async def valorant(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="Wild Rift", style=discord.ButtonStyle.blurple, custom_id="wild_rift", row=1)
    async def wild_rift(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="Ogłoszenia", style=discord.ButtonStyle.gray, custom_id="ogloszenia", row=2)
    async def ogloszenia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="Lol Newsy", style=discord.ButtonStyle.gray, custom_id="lolkowe_newsy", row=2)
    async def lolkowe_newsy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

class Not_lol(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Usuń wszystkie role", style=discord.ButtonStyle.gray, custom_id="usun_w_role", row=1)
    async def usun_w_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        all_roles = config.lol_other + config.lol_ranks + config.lol_servers + ["TFT", "LOR", "Valorant", "Dyskusje", "Użytkownik", "Nie posiadam konta w lolu", "Lol Newsy", "Ogłoszenia", "Wild Rift"]
        remove_roles = []
        if "Zweryfikowany" in str(interaction.user.roles):
            for role in interaction.user.roles:
                if str(role) in config.lol_other or str(role) in ["TFT", "LOR", "Valorant", "Dyskusje", "Lol Newsy", "Ogłoszenia", "Wild Rift"]:
                    remove_roles.append(role)
            await interaction.user.remove_roles(*remove_roles)
            await interaction.followup.send("Jesteś zweryfikowany. Bot usunął wszystkie role poza dywizją i regionem.", ephemeral=True)
        else:
            for role in interaction.user.roles:
                if str(role) in all_roles:
                    remove_roles.append(role)
            await interaction.user.remove_roles(*remove_roles)
            await interaction.followup.send("Usunąłeś wszystkie role z przyznawania ról!", ephemeral=True)

    @discord.ui.button(label="Nie posiadam konta w lolu", style=discord.ButtonStyle.gray, custom_id="npkwl", row=0)
    async def npkwl(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (has_rank_roles(interaction.user) or has_server_roles(interaction.user) or has_other_roles(interaction.user)):
            if "Zweryfikowany" in str(interaction.user.roles):
                interaction.response.send_message("Nie możesz dostać roli **Nie posiadam konta w lolu** posiadając rolę Zweryfikowany!", ephemeral=True)
            else:
                await give_other_roles(interaction, button)
        else:
            await interaction.response.send_message("Nie możesz dostać roli **Nie posiadam konta w lolu** posiadając role ligowe. Zdejmij je i spróbuj ponownie.", ephemeral=True)
    
class WerPrzycisk(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.value = 0
        self.cd = commands.CooldownMapping.from_cooldown(1.0, 420.0, key)
        self.bot = bot

    @discord.ui.button(label="Weryfikacja", style=discord.ButtonStyle.red, custom_id="weryfikacja")
    async def weryfikacja(self, interaction: discord.Interaction, button: discord.ui.Button):
        retry_after = self.cd.update_rate_limit(interaction)
        if retry_after:
            raise ButtonOnCooldown(retry_after)
        else:
            if "Zweryfikowany" not in str(interaction.user.roles):
                await interaction.response.send_modal(Weryfikacja(self.bot))
            else:
                await interaction.response.send_message("**Już jesteś zweryfikowany.** Jeśli chcesz zmienić konto, użyj komendy */usun_weryfikacje*.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        if isinstance(error, ButtonOnCooldown):
            seconds = int(error.retry_after)
            await interaction.response.send_message(f"Spróbuj ponownie za {seconds}s!", ephemeral=True)
        else:
            await super().on_error(interaction, error, item)

class Przyznawanie_Roli(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(Opcjonalne(self.bot))
        bot.add_view(Rangowe())
        bot.add_view(Serwerowe(self.bot))
        bot.add_view(Not_lol())
        bot.add_view(WerPrzycisk(self.bot))

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="przyznawanie_roli", description="Wysyła przyciski do przyznawania roli.")
    async def przyznawanie_roli(self, interaction: discord.Interaction):
        await interaction.response.send_message(content='**Role Obowiązkowe**\nDywizja:', view=Rangowe())
        await interaction.channel.send(content="Region:", view=Serwerowe(self.bot))
        await interaction.channel.send(content="»»————-\n**Role opcjonalne**", view=Opcjonalne(self.bot))
        await interaction.channel.send(content='»»————-', view=Not_lol())
        await interaction.channel.send(content="»»————-")

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="weryfikacja", description="Wysyła przycisk do weryfikacji.")
    async def weryfikacja(self, interaction: discord.Interaction):
        await interaction.response.send_message(content="»»————-\n**Weryfikacja konta w lolu**\nPrzypisuje twoje konto do discorda i automatyczne aktualizuje role wraz ze zmianą dywizji! Nikt nie widzi twojego nicku (w tym moderacja).\nWymagana by brać udział we wszelkich konkursach/giveaway'ach/turniejach oraz w rekrutacji.\n__Moderacja zastrzega sobie prawo do wymagania weryfikacji od danego użytkownika.__\n»»————-", view=WerPrzycisk(self.bot))

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="start", description="Wysyła przyciski start.")
    async def start(self, interaction: discord.Interaction):
        await interaction.response.send_message(content='**Role Obowiązkowe**\nDywizja:', view=Rangowe())
        await interaction.channel.send(content="Region:", view=Serwerowe(self.bot))
        await interaction.channel.send(content='»»————-', view=Not_lol())
        await interaction.channel.send(content='»»————-')

    @przyznawanie_roli.error
    async def przyznawanie_roliError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

    @weryfikacja.error
    async def weryfikacjaError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

    @start.error
    async def startError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Przyznawanie_Roli(bot), guild = discord.Object(id = config.guild_id))