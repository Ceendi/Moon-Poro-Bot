import discord
from discord import app_commands
from discord.ext import commands
import config
import datetime


class Przyjmij(discord.ui.View):
    def __init__(self, author, bot):
        super().__init__(timeout=86400)
        self.author: discord.Member = author
        self.bot = bot

    @discord.ui.button(label="Przyjmij", style=discord.ButtonStyle.green)
    async def przyjmij(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_content = interaction.message.content + f"\n{interaction.user.mention} przyjął to zgłoszenie!"
        await interaction.message.edit(content=new_content, view=None)
        try:
            await self.author.send(f"Twoje zgłoszenie zostało przyjęte przez moda. Odpowiednie działania zostały podjęte.")
        except discord.errors.Forbidden:
            pass
        await interaction.response.send_message("Przyjąłeś zgłoszenie!", ephemeral=True)
        async with self.bot.pool.acquire() as con:
            mod_stat = await con.fetch('SELECT * FROM mod_stats WHERE id=$1;', interaction.user.id)
            if not mod_stat:
                await con.execute("INSERT INTO mod_stats(id) VALUES($1);", interaction.user.id)
            year = datetime.date.today().year%100
            month = datetime.date.today().strftime('%m')
            column_name = "zy" + str(year) + '_m' + str(month)
            await con.execute(f'''ALTER TABLE mod_stats ADD COLUMN IF NOT EXISTS zy{year}_m{month} SMALLINT DEFAULT 0;
                                    ALTER TABLE mod_stats ADD COLUMN IF NOT EXISTS wy{year}_m{month} SMALLINT DEFAULT 0;''')
            await con.execute(f"UPDATE mod_stats SET {column_name}={column_name}+1 WHERE id=$1;", interaction.user.id)


class ZgloszenieModal(discord.ui.Modal):
    def __init__(self, title, bot):
        self.title = title
        self.bot = bot
        super().__init__()

    powod = discord.ui.TextInput(style=discord.TextStyle.long, required=True, label='Powód')

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.ticket_channel_id)
        await channel.send(content=f"@here\n**{self.title}**\n{interaction.user.mention}: {self.powod}", view=Przyjmij(interaction.user, self.bot))
        await interaction.response.send_message("Pomyślnie wysłano zgłoszenie.", ephemeral=True)


class Zgloszenie(discord.ui.Button):
    def __init__(self, title, bot):
        self.title = title
        self.bot = bot
        super().__init__(label=self.title, custom_id=self.title, style=discord.ButtonStyle.red)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ZgloszenieModal(self.title, self.bot))


class Ticket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        toxic = discord.ui.View(timeout=None)
        toxic.add_item(Zgloszenie('Toxic', self.bot))
        odwolania = discord.ui.View(timeout=None)
        odwolania.add_item(Zgloszenie('Odwołania', self.bot))
        ranga = discord.ui.View(timeout=None)
        ranga.add_item(Zgloszenie('Ranga', self.bot))
        inne = discord.ui.View(timeout=None)
        inne.add_item(Zgloszenie('Inne', self.bot))
        self.bot.add_view(toxic)
        self.bot.add_view(odwolania)
        self.bot.add_view(ranga)
        self.bot.add_view(inne)

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="skargi", description="Wysyła przyciski ze skargami.")
    async def skargi(self, interaction: discord.Interaction):
        toxic = discord.ui.View(timeout=None)
        toxic.add_item(Zgloszenie('Toxic'))
        odwolania = discord.ui.View(timeout=None)
        odwolania.add_item(Zgloszenie('Odwołania'))
        ranga = discord.ui.View(timeout=None)
        ranga.add_item(Zgloszenie('Ranga'))
        inne = discord.ui.View(timeout=None)
        inne.add_item(Zgloszenie('Inne'))
        await interaction.response.send_message(content="**Toksyczne zachowanie**\nZgłoszenie odnośnie nieodpowiedniego zachowania na kanale głosowym, bądź tekstowym. Podaj odnośnie __jakiego tyczy się użytkownika__ oraz __na jakim kanale ma miejsce.__ Czym więcej informacji tym lepiej.", view=toxic)
        await interaction.channel.send(content="\n**Odwołanie do kary i skargi na moderację**\nZgłoszenie odnośnie nieodpowiedniego potraktowania przez moderatora. Napisz co miało miejsce oraz czemu czujesz się niesprawiedliwie potraktowany. Administrator skontaktuje się z tobą oraz wyjaśni sytuację.", view=odwolania)
        await interaction.channel.send(content="\n**Nieprawdziwa ranga**\nZgłoszenie odnośnie użytkownika z nieprawidłowo ustawionymi rolami. Jeśli ktoś ma nieprawdziwą dywizję na serwerze to __podaj jego nick na discordzie wraz z tagiem, bądź ID__, resztą zajmie się moderacja.", view=ranga)
        await interaction.channel.send(content="\n**Inne**\nWszelkie inne zgłoszenie, które nie podpadają pod powyższe kategorie. ", view=inne)
        

    @skargi.error
    async def skargiError(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            await interaction.response.send_message("Nie posiadasz permisji do użycia tej komendy.", ephemeral=True)
        else:
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Ticket(bot), guild = discord.Object(id = config.guild_id))