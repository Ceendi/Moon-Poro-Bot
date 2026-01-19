import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import update_mod_stats
from utils.errors import handle_app_command_error


class PrzyjmijView(discord.ui.View):
    def __init__(self, author: discord.Member, bot: commands.Bot):
        super().__init__(timeout=86400)
        self.author = author
        self.bot = bot

    @discord.ui.button(label="✓ Przyjmij", style=discord.ButtonStyle.green)
    async def przyjmij(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_content = f"{interaction.message.content}\n✅ {interaction.user.mention} przyjął to zgłoszenie!"
        await interaction.message.edit(content=new_content, view=None)
        
        try:
            await self.author.send("Twoje zgłoszenie zostało przyjęte przez moda. Odpowiednie działania zostały podjęte.")
        except discord.errors.Forbidden:
            pass
        
        await interaction.response.send_message("Przyjąłeś zgłoszenie!", ephemeral=True)
        await update_mod_stats(self.bot.pool, interaction.user.id, 'z')


class ZgloszenieModal(discord.ui.Modal):
    def __init__(self, title: str, bot: commands.Bot):
        super().__init__(title=title, timeout=300)
        self.report_title = title
        self.bot = bot
    
    powod = discord.ui.TextInput(
        style=discord.TextStyle.long,
        required=True,
        label='Powód zgłoszenia',
        placeholder='Opisz szczegółowo sytuację...',
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.ticket_channel_id)
        
        await channel.send(
            content=f"@here\n**{self.report_title}**\n{interaction.user.mention}: {self.powod.value}",
            view=PrzyjmijView(interaction.user, self.bot)
        )
        
        await interaction.response.send_message("✅ Pomyślnie wysłano zgłoszenie. Moderacja zajmie się tym wkrótce.", ephemeral=True)


class ZgloszenieButton(discord.ui.Button):
    def __init__(self, title: str, bot: commands.Bot):
        super().__init__(label=title, custom_id=f"report_{title.lower().replace(' ', '_')}", style=discord.ButtonStyle.red)
        self.report_title = title
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ZgloszenieModal(self.report_title, self.bot))


class TicketCog(commands.Cog):
    REPORT_TYPES = [
        ("Toxic", "Toksyczne zachowanie"),
        ("Odwołania", "Odwołanie do kary i skargi na moderację"),
        ("Ranga", "Nieprawdziwa ranga"),
        ("Inne", "Inne zgłoszenia"),
    ]
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        for report_type, _ in self.REPORT_TYPES:
            view = discord.ui.View(timeout=None)
            view.add_item(ZgloszenieButton(report_type, self.bot))
            bot.add_view(view)

    @app_commands.checks.has_any_role("Administracja")
    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="skargi", description="Wysyła przyciski ze skargami")
    async def skargi(self, interaction: discord.Interaction):
        messages = [
            ("**Toksyczne zachowanie**\nZgłoszenie odnośnie nieodpowiedniego zachowania na kanale głosowym lub tekstowym. Podaj __jakiego użytkownika__ oraz __na jakim kanale__.", "Toxic"),
            ("\n**Odwołanie do kary i skargi na moderację**\nZgłoszenie odnośnie nieodpowiedniego potraktowania przez moderatora. Administrator skontaktuje się z tobą.", "Odwołania"),
            ("\n**Nieprawdziwa ranga**\nZgłoszenie użytkownika z nieprawidłowymi rolami. Podaj __nick na discordzie wraz z tagiem lub ID__.", "Ranga"),
            ("\n**Inne**\nWszelkie inne zgłoszenia, które nie podpadają pod powyższe kategorie.", "Inne"),
        ]
        
        first_view = discord.ui.View(timeout=None)
        first_view.add_item(ZgloszenieButton(messages[0][1], self.bot))
        await interaction.response.send_message(content=messages[0][0], view=first_view)
        
        for content, button_label in messages[1:]:
            view = discord.ui.View(timeout=None)
            view.add_item(ZgloszenieButton(button_label, self.bot))
            await interaction.channel.send(content=content, view=view)

    @skargi.error
    async def skargi_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if not await handle_app_command_error(interaction, error):
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot), guild=discord.Object(id=config.guild_id))