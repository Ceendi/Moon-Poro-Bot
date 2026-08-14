from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, cast

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from moon_poro.bot import MoonPoroBot
from moon_poro.models import Warning, WarningStatus
from moon_poro.permissions import moderator_only
from moon_poro.roles import find_role, member_roles_named

logger = logging.getLogger("moon_poro.warnings")


class WarningRoleUnavailable(RuntimeError):
    pass


def warning_embed(
    warning: Warning,
    role_name: str,
    *,
    status: WarningStatus | str | None = None,
) -> discord.Embed:
    raw_status: Any = status if status is not None else getattr(warning, "status", "ACTIVE")
    resolved_status = WarningStatus(str(raw_status))
    suffixes = {
        WarningStatus.EXPIRED: "wygasł",
        WarningStatus.REVOKED: "cofnięty",
        WarningStatus.SUPERSEDED: "zastąpiony",
    }
    suffix = suffixes.get(resolved_status)
    colour = (
        discord.Colour.red()
        if resolved_status is WarningStatus.ACTIVE
        else discord.Colour(0x607D8B)
    )
    title = f"{role_name} — {suffix}" if suffix else role_name
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
    if resolved_status is not WarningStatus.ACTIVE:
        labels = {
            WarningStatus.EXPIRED: "Wygasł (EXPIRED)",
            WarningStatus.REVOKED: "Cofnięty (REVOKED)",
            WarningStatus.SUPERSEDED: "Zastąpiony (SUPERSEDED)",
        }
        embed.add_field(name="Status", value=labels[resolved_status], inline=False)
    embed.set_footer(text=f"ID kary: {warning.id}")
    return embed


class WarningsCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self._member_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.reconcile_warnings.change_interval(
            seconds=bot.settings.warning_reconcile_interval_seconds
        )
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
        if level is not None and desired is None:
            role_name = self.bot.settings.warn_roles[level]
            raise WarningRoleUnavailable(f"Configured warning role is unavailable: {role_name}")
        to_remove = [role for role in existing if role != desired]
        if to_remove:
            await member.remove_roles(*to_remove, reason="Synchronizacja aktywnej kary")
        if desired is not None and desired not in member.roles:
            await member.add_roles(desired, reason="Synchronizacja aktywnej kary")

    async def _sync_member_warning_role(self, member: discord.Member) -> None:
        warning = await self.bot.warnings.get_active(member.guild.id, member.id)
        await self._set_warning_role(member, warning.level if warning is not None else None)
        await self.bot.warnings.acknowledge_role_sync(member.guild.id, member.id)

    async def _sync_audit_message(self, channel: discord.abc.Messageable, warning: Warning) -> None:
        role_name = self.bot.settings.warn_roles[warning.level]
        message = cast(Any, channel).get_partial_message(warning.message_id)
        await message.edit(
            content=None,
            embed=warning_embed(warning, role_name),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.bot.warnings.acknowledge_audit_sync(warning.guild_id, warning.message_id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild.id != self.bot.settings.guild_id:
            return
        async with self._member_locks[(member.guild.id, member.id)]:
            try:
                await self._sync_member_warning_role(member)
            except (discord.HTTPException, WarningRoleUnavailable):
                logger.exception("Could not synchronize warning role on join for %s", member.id)
            except Exception:
                logger.exception("Could not load warning state on join for %s", member.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or after.guild.id != self.bot.settings.guild_id:
            return
        configured_names = set(self.bot.settings.warn_roles.values())
        before_roles = {
            role.id for role in member_roles_named(before, configured_names, self.bot.settings)
        }
        after_roles = {
            role.id for role in member_roles_named(after, configured_names, self.bot.settings)
        }
        if before_roles == after_roles:
            return

        async with self._member_locks[(after.guild.id, after.id)]:
            get_member = getattr(after.guild, "get_member", None)
            current = get_member(after.id) if callable(get_member) else after
            current = current or after
            try:
                await self._sync_member_warning_role(current)
            except (discord.HTTPException, WarningRoleUnavailable):
                logger.exception("Could not protect warning role for %s", after.id)
            except Exception:
                logger.exception("Could not load warning state after role update for %s", after.id)

    @tasks.loop(seconds=300, reconnect=True)
    async def reconcile_warnings(self) -> None:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            return

        try:
            await self.bot.warnings.expire_due(guild.id)
            warnings = await self.bot.warnings.list_for_reconciliation(guild.id)
        except Exception:
            logger.exception("Could not load warning reconciliation data; next run will retry")
            return

        user_ids = {warning.discord_user_id for warning in warnings}
        for user_id in user_ids:
            async with self._member_locks[(guild.id, user_id)]:
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        await self.bot.warnings.acknowledge_role_sync(guild.id, user_id)
                    except Exception:
                        logger.exception("Could not acknowledge absent warning member %s", user_id)
                    continue
                try:
                    await self._sync_member_warning_role(member)
                except (discord.HTTPException, WarningRoleUnavailable):
                    logger.exception("Could not reconcile warning role for %s", user_id)
                except Exception:
                    logger.exception("Could not load warning state for %s", user_id)

        try:
            warnings = await self.bot.warnings.list_for_reconciliation(guild.id)
        except Exception:
            logger.exception("Could not reload pending warning audits; next run will retry")
            return
        pending_by_message: dict[int, list[Warning]] = defaultdict(list)
        for warning in warnings:
            if warning.audit_sync_pending:
                pending_by_message[warning.message_id].append(warning)
        if not pending_by_message:
            return

        channel_id = self.bot.settings.warn_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning(
                "Warning audit channel %s is unavailable; %s update(s) remain pending",
                channel_id,
                len(pending_by_message),
            )
            return

        for candidates in pending_by_message.values():
            warning = max(candidates, key=lambda item: item.id)
            try:
                await self._sync_audit_message(channel, warning)
            except discord.HTTPException:
                logger.exception(
                    "Could not update warning audit message %s; it will be retried",
                    warning.message_id,
                )
            except Exception:
                logger.exception(
                    "Could not acknowledge warning audit message %s; it will be retried",
                    warning.message_id,
                )

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

        async with self._member_locks[(interaction.guild.id, uzytkownik.id)]:
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
                    try:
                        await provisional.delete()
                    except discord.HTTPException:
                        logger.exception("Could not remove unused provisional warning message")
                raise

            role_name = self.bot.settings.warn_roles[warning.level]
            message = provisional or channel.get_partial_message(warning.message_id)
            sync_failed = False
            try:
                await message.edit(
                    content=None,
                    embed=warning_embed(warning, role_name),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.bot.warnings.acknowledge_audit_sync(warning.guild_id, warning.message_id)
            except discord.HTTPException:
                sync_failed = True
                logger.exception(
                    "Could not update audit message for warning %s; it will be retried",
                    warning.id,
                )
            except Exception:
                sync_failed = True
                logger.exception(
                    "Could not acknowledge audit sync for warning %s; it will be retried",
                    warning.id,
                )
            try:
                await self._sync_member_warning_role(uzytkownik)
            except (discord.HTTPException, WarningRoleUnavailable):
                sync_failed = True
                logger.exception(
                    "Could not apply warning role for %s; it will be retried", warning.id
                )
            except Exception:
                sync_failed = True
                logger.exception(
                    "Could not acknowledge role sync for warning %s; it will be retried",
                    warning.id,
                )
            suffix = (
                " Synchronizacja Discord zostanie automatycznie ponowiona." if sync_failed else ""
            )
            await interaction.followup.send(
                f"{uzytkownik.mention} otrzymał karę **{role_name}**.{suffix}",
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
        async with self._member_locks[(interaction.guild.id, uzytkownik.id)]:
            result = await self.bot.warnings.revert(interaction.guild_id or 0, uzytkownik.id)
            if result is None:
                await interaction.followup.send("Użytkownik nie ma aktywnej kary.", ephemeral=True)
                return
            current, previous = result
            channel_id = self.bot.settings.warn_channel_id
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            sync_failed = False
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await self._sync_audit_message(channel, current)
                except discord.HTTPException:
                    sync_failed = True
                    logger.exception(
                        "Could not mark warning %s as revoked; it will be retried", current.id
                    )
                except Exception:
                    sync_failed = True
                    logger.exception(
                        "Could not acknowledge revoked warning %s; it will be retried", current.id
                    )
            else:
                sync_failed = True
                logger.warning(
                    "Warning audit channel %s is unavailable; revoke audit remains pending",
                    channel_id,
                )
            try:
                await self._sync_member_warning_role(uzytkownik)
            except (discord.HTTPException, WarningRoleUnavailable):
                sync_failed = True
                logger.exception(
                    "Could not synchronize reverted warning role for %s; it will be retried",
                    current.id,
                )
            except Exception:
                sync_failed = True
                logger.exception(
                    "Could not acknowledge reverted warning role for %s; it will be retried",
                    current.id,
                )
            suffix = (
                " Synchronizacja Discord zostanie automatycznie ponowiona." if sync_failed else ""
            )
            if previous:
                await interaction.followup.send(
                    f"Cofnięto karę do poziomu "
                    f"**{self.bot.settings.warn_roles[previous.level]}**.{suffix}"
                )
            else:
                await interaction.followup.send(f"Cofnięto aktywną karę.{suffix}")


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(WarningsCog(bot))
