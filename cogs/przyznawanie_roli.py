import discord
from discord import app_commands
from discord.ext import commands
import config
from discord.utils import get
from functions import has_rank_roles, has_server_roles, has_other_roles


async def give_other_roles(interaction: discord.Interaction, button: discord.ui.Button):
    role = get(interaction.guild.roles, name=button.label)
    if role in interaction.user.roles:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(f"Usunąłeś rolę **{str(role)}**", ephemeral=True)
        if "Dyskusje" in str(interaction.user.roles) and str(role) == "Nie posiadam konta w lolu":
            dyskusje = get(interaction.guild.roles, name="Dyskusje")
            await interaction.user.remove_roles(dyskusje)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Otrzymałeś rolę **{str(role)}**", ephemeral=True)


async def give_league_roles(interaction: discord.Interaction, button: discord.ui.Button):
    if "Nie posiadam konta w lolu" in str(interaction.user.roles):
        await interaction.response.send_message(f"Nie możesz dostać roli ligowej posiadając rolę **Nie posiadam konta w lolu**.", ephemeral=True)
        return

    role = get(interaction.guild.roles, name=button.label)

    if role in interaction.user.roles:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(f"Usunąłeś rolę **{str(role)}**", ephemeral=True)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Otrzymałeś rolę **{str(role)}**", ephemeral=True)

    await give_uzytkownik(interaction)


async def give_rank_role(interaction: discord.Interaction, select: discord.ui.Select):
    if "Nie posiadam konta w lolu" in str(interaction.user.roles):
        await interaction.response.send_message(f"Nie możesz dostać roli ligowej posiadając rolę **Nie posiadam konta w lolu**.", ephemeral=True)
        return

    role = get(interaction.guild.roles, name=select.values[0])
    print(123)
    previous_rank_role = None
    for r in interaction.user.roles:
        if str(r) in config.lol_ranks:
            previous_rank_role = get(interaction.guild.roles, name=str(r))
            break

    if previous_rank_role:
        await interaction.user.remove_roles(previous_rank_role)
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Otrzymałeś rolę **{str(role)}**", ephemeral=True)
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"Otrzymałeś rolę **{str(role)}**", ephemeral=True)

    await give_uzytkownik(interaction)


async def give_uzytkownik(interaction: discord.Interaction):
    uzytkownik = get(interaction.guild.roles, name="Użytkownik")
    user = interaction.guild.get_member(interaction.user.id) #need to refresh user to get actual roles

    if has_server_roles(user) and has_rank_roles(user):
        if uzytkownik not in user.roles:
            await user.add_roles(uzytkownik)
    elif uzytkownik in user.roles:
        await user.remove_roles(uzytkownik)
        if "Dyskusje" in str(user.roles):
            dyskusje = get(interaction.guild.roles, name="Dyskusje")
            await user.remove_roles(dyskusje)


class Przyciski(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        max_values=1,
        placeholder="Wybierz swoją rangę...",
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
        await give_rank_role(interaction, select)

    @discord.ui.button(label="EUNE", style=discord.ButtonStyle.red, custom_id="eune", row=1)
    async def eune(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="EUW", style=discord.ButtonStyle.red, custom_id="euw", row=1)
    async def euw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="NA", style=discord.ButtonStyle.red, custom_id="na", row=1)
    async def na(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="Top", style=discord.ButtonStyle.green, custom_id="top", row=2)
    async def top(self, interaction: discord.Interaction, button: discord.ui.Button):
       await give_league_roles(interaction, button)

    @discord.ui.button(label="Jungle", style=discord.ButtonStyle.green, custom_id="jungle", row=2)
    async def jungle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="Mid", style=discord.ButtonStyle.green, custom_id="mid", row=2)
    async def mid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="ADC", style=discord.ButtonStyle.green, custom_id="adc", row=2)
    async def adc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="Support", style=discord.ButtonStyle.green, custom_id="support", row=2)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="Weryfikacja", style=discord.ButtonStyle.blurple, custom_id="weryfikacja", row=3)
    async def weryfikacja(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message('Tu bedzie weryfikacja :)', ephemeral=True)

    @discord.ui.button(label="Szukam gry", style=discord.ButtonStyle.blurple, custom_id="szukam_gry", row=3)
    async def szukam_gry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_league_roles(interaction, button)

    @discord.ui.button(label="TFT", style=discord.ButtonStyle.gray, custom_id="tft", row=4)
    async def tft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="LOR", style=discord.ButtonStyle.gray, custom_id="lor", row=4)
    async def lor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="Valorant", style=discord.ButtonStyle.gray, custom_id="valorant", row=4)
    async def valorant(self, interaction: discord.Interaction, button: discord.ui.Button):
        await give_other_roles(interaction, button)

    @discord.ui.button(label="Dyskusje", style=discord.ButtonStyle.gray, custom_id="dyskusje", row=4)
    async def dyskusje(self, interaction: discord.Interaction, button: discord.ui.Button):
        if "Użytkownik" in str(interaction.user.roles) or "Nie posiadam konta w lolu" in str(interaction.user.roles):
            await give_other_roles(interaction, button)
        else:
            await interaction.response.send_message("Żeby mieć rolę **Dyskusje**, potrzebujesz roli rangowej + serwerowej lub **Nie posiadam konta w lolu**.", ephemeral=True)

    @discord.ui.button(label="Nie posiadam konta w lolu", style=discord.ButtonStyle.gray, custom_id="npkwl", row=4)
    async def npkwl(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (has_rank_roles(interaction.user) or has_server_roles(interaction.user) or has_other_roles(interaction.user)):
            await give_other_roles(interaction, button)
        else:
            await interaction.response.send_message("Nie możesz dostać roli **Nie posiadam konta w lolu** posiadając inne role z lola.", ephemeral=True)


class Role(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(Przyciski())

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="przyznawanie_roli", description="Wysyła przyciski do przyznawania roli.")
    async def przyznawanie_roli(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=Przyciski())

    @przyznawanie_roli.error
    async def przyznawanie_roliError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message("Nie posiadasz permisji do używania tej komendy!", ephemeral=True)
        else:
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Role(bot), guild = discord.Object(id = config.guild_id))