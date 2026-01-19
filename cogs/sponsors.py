import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import get

import config


class PersonButton(discord.ui.Button):
    def __init__(self, member: discord.Member):
        super().__init__(label=str(member), style=discord.ButtonStyle.red)
        self.member = member

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.drzez_vc_channel_id)
        if self.member in channel.members:
            await self.member.move_to(channel=None)
        await channel.set_permissions(self.member, connect=False)
        await interaction.response.send_message(f"✅ Kicknięto {self.member.mention} z kanału.", ephemeral=True)


class SponsorsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vc_start_time: Optional[float] = None

    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="vban", description="Usuwa dostęp do wbijania na kanał")
    async def vban(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.drzez_vc_channel_id)
        members = [m for m in channel.members if m.id != config.drzez_user_id]
        if not members:
            await interaction.response.send_message("Nie znaleziono osób na kanale.", ephemeral=True)
            return
        view = discord.ui.View(timeout=60)
        for member in members:
            view.add_item(PersonButton(member))
        await interaction.response.send_message("Wybierz osobę do kicknięcia:", view=view, ephemeral=True)

    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="vuban", description="Cofa usunięty dostęp do kanału")
    @app_commands.describe(uzytkownik="Osoba, której oddajesz dostęp")
    async def vunban(self, interaction: discord.Interaction, uzytkownik: discord.Member):
        channel = interaction.guild.get_channel(config.drzez_vc_channel_id)
        await channel.set_permissions(uzytkownik, connect=True)
        await interaction.response.send_message("✅ Oddano dostęp do kanału!", ephemeral=True)

    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="vopen", description="Otwiera dostęp do kanału")
    async def vopen(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.drzez_vc_channel_id)
        uzytkownik_role = get(interaction.guild.roles, name="Użytkownik")
        if uzytkownik_role:
            await channel.set_permissions(uzytkownik_role, view_channel=True, connect=True)
        await interaction.response.send_message("✅ Otwarto kanał!", ephemeral=True)

    @app_commands.guilds(discord.Object(id=config.guild_id))
    @app_commands.command(name="vclose", description="Zamyka dostęp do kanału")
    async def vclose(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(config.drzez_vc_channel_id)
        uzytkownik_role = get(interaction.guild.roles, name="Użytkownik")
        if uzytkownik_role:
            await channel.set_permissions(uzytkownik_role, connect=False, view_channel=True)
        await interaction.response.send_message("✅ Zamknięto kanał!", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if after.channel == before.channel:
            return
        await self._handle_proxy_vc(member, before, after)
        await self._handle_drzez_vc(member, before, after)

    async def _handle_proxy_vc(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if after.channel and after.channel.id == config.proxy_vc_channel_id and len(after.channel.members) == 1:
            self.vc_start_time = time.time()
            return
        if before.channel and before.channel.id == config.proxy_vc_channel_id and not before.channel.members:
            if self.vc_start_time is None:
                return
            duration = int(time.time() - self.vc_start_time)
            self.vc_start_time = None
            await self._log_proxy_time(member.guild, duration)

    async def _log_proxy_time(self, guild: discord.Guild, duration: int):
        data = await self.bot.pool.fetch("SELECT * FROM proxy_vc;")
        if not data:
            return
        time_100 = duration + data[0][1]
        time_whole = duration + data[0][0]
        log_channel = guild.get_channel(config.proxy_log_channel_id)
        if duration < 60:
            time_msg = f"Rozmowa trwała {duration} sekund."
        elif duration < 3600:
            time_msg = f"Rozmowa trwała {duration // 60} minut."
        else:
            time_msg = f"Rozmowa trwała {duration // 3600} godzin i {(duration % 3600) // 60} minut."
        if log_channel:
            await log_channel.send(time_msg)
        if time_100 > 360000:
            total_hours = (time_whole // 3600) - (time_whole // 3600) % 100
            await self.bot.pool.execute("UPDATE proxy_vc SET time=$1, message_time=$2;", time_whole, time_100 - 360000)
            general_channel = guild.get_channel(config.general_channel_id)
            if general_channel:
                await general_channel.send(f"🎉 Proxy i Talone siedzieli na VC **{total_hours}** godzin!")
        else:
            await self.bot.pool.execute("UPDATE proxy_vc SET time=$1, message_time=$2;", time_whole, time_100)

    async def _handle_drzez_vc(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id == config.drzez_user_id:
            return
        drzez = member.guild.get_member(config.drzez_user_id)
        if not drzez:
            return
        try:
            if after.channel and after.channel.id == config.drzez_vc_channel_id:
                await drzez.send(f"👋 {member.mention} wbił na VC!")
            elif before.channel and before.channel.id == config.drzez_vc_channel_id:
                await drzez.send(f"👋 {member.mention} wyszedł z VC!")
        except discord.errors.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SponsorsCog(bot), guild=discord.Object(id=config.guild_id))