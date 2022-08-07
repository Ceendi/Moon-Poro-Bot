import discord
from discord import app_commands
from discord.ext import commands
import config


class Przyjmij(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=43200)
        self.author: discord.Member = author

    @discord.ui.button(label="Przyjmij", style=discord.ButtonStyle.green)
    async def przyjmij(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_content = interaction.message.content + f"\n{interaction.user.mention} przyjął to zgłoszenie!"
        await interaction.message.edit(content=new_content, view=None)
        try:
            await self.author.send(f"Twoje zgłoszenie zostało przyjęte przez moda. Odpowiednie działania zostały podjęte.")
        except discord.errors.Forbidden:
            pass
        await interaction.response.send_message("Przyjąłeś zgłoszenie!", ephemeral=True)


class ZgloszenieModal(discord.ui.Modal):
    def __init__(self, title):
        self.title = title
        super().__init__()

    powod = discord.ui.TextInput(style=discord.TextStyle.long, required=True, label='Powód')

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.ticket_channel_id)
        await channel.send(content=f"@here\n{interaction.user.mention}: **{self.title}**\n{self.powod}", view=Przyjmij(interaction.user))
        await interaction.response.send_message("Pomyślnie wysłano zgłoszenie.", ephemeral=True)


class Zgloszenie(discord.ui.Button):
    def __init__(self, title):
        self.title = title
        super().__init__(label=self.title, custom_id=self.title, style=discord.ButtonStyle.red)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ZgloszenieModal(self.title))


class Ticket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        toxic = discord.ui.View()
        toxic.add_item(Zgloszenie('Toxic'))
        odwolania = discord.ui.View()
        odwolania.add_item(Zgloszenie('Odwołania'))
        ranga = discord.ui.View()
        ranga.add_item(Zgloszenie('Ranga'))
        inne = discord.ui.View()
        inne.add_item(Zgloszenie('Inne'))
        self.bot.add_view(toxic)
        self.bot.add_view(odwolania)
        self.bot.add_view(ranga)
        self.bot.add_view(inne)

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="skargi", description="Wysyła przyciski ze skargami.")
    async def skargi(self, interaction: discord.Interaction):
        toxic = discord.ui.View()
        toxic.add_item(Zgloszenie('Toxic'))
        odwolania = discord.ui.View()
        odwolania.add_item(Zgloszenie('Odwołania'))
        ranga = discord.ui.View()
        ranga.add_item(Zgloszenie('Ranga'))
        inne = discord.ui.View()
        inne.add_item(Zgloszenie('Inne'))
        await interaction.response.send_message(content="**Toksyczne zachowanie**\nZgłoszenie odnośnie nieodpowiedniego zachowania na kanale głosowym, bądź tekstowym. Podaj odnośnie jakiego tyczy się użytkownika oraz na jakim kanale ma miejsce. Czym więcej informacji tym lepiej.", view=toxic)
        await interaction.channel.send(content="\n**Odwołanie do kary i skargi na modów**\nZgłoszenie odnośnie nieodpowiedniego potraktowania przez moderatora. Napisz co miało miejsce oraz czemu czujesz się niesprawiedliwie potraktowany. Administrator skontaktuje się z tobą oraz wyjaśni sytuację.", view=odwolania)
        await interaction.channel.send(content="\n**Nieprawdziwa ranga**\nZgłoszenie odnośnie użytkownika z nieprawidłowo ustawionymi rolami. Jeśli ktoś ma nieprawdziwą dywizję na serwerze to podaj jego nick na discordzie wraz z tagiem, bądź ID, resztą zajmie się moderacja.", view=ranga)
        await interaction.channel.send(content="\n**Inne**\nWszelkie inne zgłoszenie, które nie podpadają pod powyższe kategorie.", view=inne)
        

    @skargi.error
    async def skargiError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Ticket(bot), guild = discord.Object(id = config.guild_id))