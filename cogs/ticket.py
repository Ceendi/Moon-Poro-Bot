import discord
from discord import app_commands
from discord.ext import commands
import config


class Przyjmij(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=43200)

    @discord.ui.button(label="Przyjmij", style=discord.ButtonStyle.green)
    async def przyjmij(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_content = interaction.message.content + f"\n{interaction.user.mention} przyjął to zgłoszenie!"
        await interaction.message.edit(content=new_content, view=None)
        await interaction.response.send_message("Przyjąłeś zgłoszenie!", ephemeral=True)


class Ticket(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.guilds(discord.Object(id = config.guild_id))
    @app_commands.command(name="skarga", description="Wysyła skargę do moderacji.")
    @app_commands.describe(powod = "Jaką rzecz chcesz zgłosić moderacji?")
    async def ticket(self, interaction: discord.Interaction, powod: str):
        channel = interaction.guild.get_channel(config.ticket_channel_id)
        await channel.send(f"@here\n{interaction.user.mention}: {powod}", view=Przyjmij())
        await interaction.response.send_message("Pomyślnie wysłano zgłoszenie!", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Ticket(bot), guild = discord.Object(id = config.guild_id))