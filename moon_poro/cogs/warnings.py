from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from moon_poro.bot import MoonPoroBot
from moon_poro.models import Warning
from moon_poro.permissions import moderator_only
from moon_poro.roles import find_role, member_roles_named

logger = logging.getLogger("moon_poro.warnings")


def warning_embed(warning: Warning, role_name: str, *, expired: bool = False) -> discord.Embed:
    colour = discord.Colour(0x607D8B) if expired else discord.Colour.red()
    title = f"{role_name} — wygasł" if expired else role_name
    embed = discord.Embed(
        title=title,
        description=f"Punkty regulaminu: {warning.reasons}",
        colour=colour,
    )
    if warning.description:
        embed.add_field(name="Opis", value=warning.description[:1024], inline=False)
    embed.add_field(name="Data otrzymania", value=f"<t:{int(warning.starts_at.timestamp())}:F>")
    embed.add_field(name="Data zakończenia", value=f"<t:{int(warning.expires_at.timestamp())}:F>")
    embed.add_field(name="Użytkownik", value=f"<@{warning.discord_user_id}>", inline=False)
    moderators = ", ".join(f"<@{item.moderator_id}>" for item in warning.moderators) or "brak"
    embed.add_field(name="Moderatorzy", value=moderators, inline=False)
    embed.set_footer(text=f"ID kary: {warning.id}")
    return embed


class WarningsCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self._member_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.reconcile_warnings.start()

    async def cog_unload(self) -> None:
        self.reconcile_warnings.cancel()

    def _duration_by_level(self) -> dict[int, int]:
        settings = self.bot.settings
        return {
            level: settings.warn_days[role_name] for level, role_name in settings.warn_roles.items()
        }

    def _role(self, guild: discord.Guild, level: int) -> discord.Role | None:
        return find_role(guild, self.bot.settings.warn_roles[level], self.bot.settings)

    async def _set_warning_role(self, member: discord.Member, level: int | None) -> None:
        configured_names = set(self.bot.settings.warn_roles.values())
        existing = member_roles_named(member, configured_names, self.bot.settings)
        desired = self._role(member.guild, level) if level is not None else None
        to_remove = [role for role in existing if role != desired]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Synchronizacja aktywnej kary")
        if desired is not None and desired not in member.roles:
            await member.add_roles(desired, reason="Synchronizacja aktywnej kary")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        warning = await self.bot.warnings.get_active(member.guild.id, member.id)
        if warning is not None:
            await self._set_warning_role(member, warning.level)

    @tasks.loop(hours=1, reconnect=True)
    async def reconcile_warnings(self) -> None:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        channel_id = self.bot.settings.warn_channel_id
        if guild is None or channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return

        try:
            expired_ids = {warning.id for warning in await self.bot.warnings.list_expired(guild.id)}
            active_warnings = await self.bot.warnings.list_active(guild.id)
        except Exception:
            logger.exception("Could not load warning reconciliation data; next run will retry")
            return
        for warning in active_warnings:
            member = guild.get_member(warning.discord_user_id)
            if warning.id not in expired_ids:
                if member is not None:
                    try:
                        await self._set_warning_role(member, warning.level)
                    except discord.HTTPException:
                        logger.exception("Could not reconcile warning role for %s", member.id)
                continue
            try:
                role_name = self.bot.settings.warn_roles[warning.level]
                with suppress(discord.NotFound):
                    await channel.get_partial_message(warning.message_id).edit(
                        embed=warning_embed(warning, role_name, expired=True)
                    )
                if member is not None:
                    await self._set_warning_role(member, None)
                await self.bot.warnings.mark_expired(warning.id)
            except discord.HTTPException:
                logger.exception("Could not expire warning %s; it will be retried", warning.id)

    @reconcile_warnings.before_loop
    async def before_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    @reconcile_warnings.error
    async def reconcile_error(self, error: BaseException) -> None:
        logger.exception("Warning reconciliation loop failed", exc_info=error)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.choices(
        typ=[
            Choice(name="Warn", value=1),
            Choice(name="Warn 2", value=2),
            Choice(name="TIMEOUT", value=3),
        ]
    )
    @app_commands.command(name="w", description="Nadaje lub eskaluje karę użytkownika")
    async def warn(
        self,
        interaction: discord.Interaction,
        typ: int,
        uzytkownik: discord.Member,
        powod: app_commands.Range[int, 1, 99],
        dodatkowy_powod: app_commands.Range[int, 1, 99] | None = None,
        opis: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        await interaction.response.defer()
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.", ephemeral=True)
            return
        if uzytkownik.bot:
            await interaction.followup.send("Nie można nadać kary botowi.", ephemeral=True)
            return
        if (
            uzytkownik.top_role >= interaction.user.top_role
            and interaction.guild.owner_id != interaction.user.id
        ):
            await interaction.followup.send(
                "Nie możesz ukarać użytkownika z równą lub wyższą rolą.",
                ephemeral=True,
            )
            return

        async with self._member_locks[uzytkownik.id]:
            active = await self.bot.warnings.get_active(interaction.guild_id or 0, uzytkownik.id)
            if active is not None and active.level >= 3:
                await interaction.followup.send(
                    "Użytkownik ma już najwyższy poziom kary.", ephemeral=True
                )
                return
            channel_id = self.bot.settings.warn_channel_id
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.abc.Messageable):
                await interaction.followup.send(
                    "Kanał kar jest źle skonfigurowany.", ephemeral=True
                )
                return

            reasons = f"{powod}/{dodatkowy_powod}" if dodatkowy_powod else str(powod)
            provisional = None
            message_id = active.message_id if active else 0
            if active is None:
                provisional = await channel.send("Zapisywanie kary…")
                message_id = provisional.id
            try:
                warning = await self.bot.warnings.issue(
                    guild_id=interaction.guild_id or 0,
                    user_id=uzytkownik.id,
                    requested_level=typ,
                    reasons=reasons,
                    description=opis,
                    moderator_id=interaction.user.id,
                    message_id=message_id,
                    duration_by_level=self._duration_by_level(),
                )
            except Exception:
                if provisional is not None:
                    await provisional.delete()
                raise

            role_name = self.bot.settings.warn_roles[warning.level]
            message = provisional or channel.get_partial_message(warning.message_id)
            await message.edit(
                content=None,
                embed=warning_embed(warning, role_name),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._set_warning_role(uzytkownik, warning.level)
            await interaction.followup.send(
                f"{uzytkownik.mention} otrzymał karę **{role_name}**.",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="cw", description="Cofa ostatni poziom kary")
    async def revert_warning(
        self, interaction: discord.Interaction, uzytkownik: discord.Member
    ) -> None:
        await interaction.response.defer()
        if interaction.guild is None:
            await interaction.followup.send("Ta komenda działa tylko na serwerze.", ephemeral=True)
            return
        async with self._member_locks[uzytkownik.id]:
            result = await self.bot.warnings.revert(interaction.guild_id or 0, uzytkownik.id)
            if result is None:
                await interaction.followup.send("Użytkownik nie ma aktywnej kary.", ephemeral=True)
                return
            current, previous = result
            channel_id = self.bot.settings.warn_channel_id
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            if isinstance(channel, discord.abc.Messageable):
                message = channel.get_partial_message(current.message_id)
                if previous is None:
                    with suppress(discord.NotFound):
                        await message.delete()
                else:
                    await message.edit(
                        embed=warning_embed(previous, self.bot.settings.warn_roles[previous.level]),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            await self._set_warning_role(uzytkownik, previous.level if previous else None)
            if previous:
                await interaction.followup.send(
                    f"Cofnięto karę do poziomu **{self.bot.settings.warn_roles[previous.level]}**."
                )
            else:
                await interaction.followup.send("Usunięto aktywną karę.")


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(WarningsCog(bot))
