from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from moon_poro.bot import MoonPoroBot
from moon_poro.models import VerificationLink, VerificationSession
from moon_poro.permissions import administrator_only
from moon_poro.riot import (
    API_SERVERS,
    SERVER_TRANSLATION,
    RiotAPIUnavailable,
    get_discord_rank_role,
    get_rank_from_leagues,
    riot_api_call,
)
from moon_poro.roles import find_role, member_has_role, member_roles_named

logger = logging.getLogger("moon_poro.verification")

type RiotPayload = dict[str, Any]
type LeagueEntries = list[RiotPayload]


def _lookup_reason_choices() -> list[Choice[str]]:
    return [
        Choice(name="Moderacja", value="Moderacja"),
        Choice(name="Pomoc użytkownikowi", value="Pomoc użytkownikowi"),
        Choice(name="Podejrzenie multikonta", value="Podejrzenie multikonta"),
        Choice(name="Korekta danych", value="Korekta danych"),
    ]


def _server_choices() -> list[Choice[str]]:
    return [
        Choice(name="EUNE", value="EUN1"),
        Choice(name="EUW", value="EUW1"),
        Choice(name="NA", value="NA1"),
    ]


def _interaction_user_id(interaction: discord.Interaction) -> int:
    return interaction.user.id


async def _get_leagues(bot: MoonPoroBot, platform: str, puuid: str) -> LeagueEntries:
    leagues: LeagueEntries | None = await riot_api_call(
        lambda: bot.riot_client.get_lol_league_v4_entries_by_puuid(
            region=platform,
            puuid=puuid,
        ),
        not_found=[],
    )
    return leagues or []


async def _apply_verified_roles(
    bot: MoonPoroBot,
    member: discord.Member,
    platform: str,
    leagues: LeagueEntries,
) -> None:
    settings = bot.settings
    rank_role = get_discord_rank_role(member.guild, get_rank_from_leagues(leagues), settings)
    server_name = SERVER_TRANSLATION[platform]
    server_role = find_role(member.guild, server_name, settings)
    verified_role = find_role(member.guild, settings.verified_role_name, settings)
    member_role = find_role(member.guild, settings.member_role_name, settings)

    managed_names = set(settings.lol_ranks + settings.lol_servers)
    desired = {role for role in (rank_role, server_role) if role is not None}
    current_managed = set(member_roles_named(member, managed_names, settings))
    to_remove = current_managed - desired
    to_add = desired - set(member.roles)
    to_add.update(
        role
        for role in (verified_role, member_role)
        if role is not None and role not in member.roles
    )
    if to_remove:
        await member.remove_roles(*to_remove, reason="Synchronizacja weryfikacji Riot")
    if to_add:
        await member.add_roles(*to_add, reason="Synchronizacja weryfikacji Riot")


async def _remove_verified_roles(bot: MoonPoroBot, member: discord.Member, *, reason: str) -> None:
    settings = bot.settings
    managed_names = set(settings.lol_ranks + settings.lol_servers + [settings.verified_role_name])
    to_remove = member_roles_named(member, managed_names, settings)
    if to_remove:
        await member.remove_roles(*to_remove, reason=reason)


def _cached_rank_leagues(link: VerificationLink) -> LeagueEntries | None:
    if link.last_known_rank is None:
        return None
    return [{"queueType": "RANKED_SOLO_5x5", "tier": link.last_known_rank}]


async def _refresh_next_verified(cog: VerificationCog) -> None:
    bot = cog.bot
    guild = bot.get_guild(bot.settings.guild_id)
    if guild is None:
        return
    links = await bot.verifications.claim_due_rank_refreshes(
        guild.id,
        limit=1,
        claim_timeout_seconds=bot.settings.rank_refresh_claim_timeout_seconds,
    )
    if not links:
        return

    link = links[0]
    member = guild.get_member(link.discord_user_id)
    if member is None:
        await bot.verifications.defer_rank_refresh(
            guild.id,
            link.discord_user_id,
            delay_seconds=bot.settings.rank_refresh_interval_hours * 3600,
        )
        return
    if link.puuid is None:
        return

    try:
        leagues = await _get_leagues(bot, link.platform, link.puuid)
        await cog.apply_verified_roles(member, link.platform, leagues)
    except RiotAPIUnavailable:
        delay = await bot.verifications.retry_rank_refresh(
            guild.id,
            link.discord_user_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
        )
        logger.warning(
            "Could not refresh Riot rank for Discord user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return
    except discord.HTTPException:
        delay = await bot.verifications.retry_rank_refresh(
            guild.id,
            link.discord_user_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
        )
        logger.exception(
            "Could not refresh Discord roles for user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return

    await bot.verifications.complete_rank_refresh(
        guild.id,
        link.discord_user_id,
        rank_tier=get_rank_from_leagues(leagues).upper(),
        refresh_interval_hours=bot.settings.rank_refresh_interval_hours,
    )


async def _restore_cached_roles_on_join(cog: VerificationCog, member: discord.Member) -> None:
    if member.guild.id != cog.bot.settings.guild_id:
        return
    link = await cog.bot.verifications.get_by_user(member.guild.id, member.id)
    if link is None or not link.puuid:
        return
    leagues = _cached_rank_leagues(link)
    if leagues is None:
        await cog.bot.verifications.schedule_rank_refresh_now(member.guild.id, member.id)
        return
    try:
        await cog.apply_verified_roles(member, link.platform, leagues)
    except discord.HTTPException:
        logger.exception("Could not restore cached verification roles for %s", member.id)


async def _reconcile_cached_role_change(
    cog: VerificationCog,
    before: discord.Member,
    after: discord.Member,
) -> None:
    if after.guild.id != cog.bot.settings.guild_id or after.id in cog._managed_role_updates:
        return
    settings = cog.bot.settings
    protected_names = set(
        settings.lol_ranks
        + settings.lol_servers
        + [settings.verified_role_name, settings.member_role_name]
    )
    before_roles = set(member_roles_named(before, protected_names, settings))
    after_roles = set(member_roles_named(after, protected_names, settings))
    if before_roles == after_roles:
        return

    link = await cog.bot.verifications.get_by_user(after.guild.id, after.id)
    if link is None or not link.puuid:
        return
    leagues = _cached_rank_leagues(link)
    if leagues is not None:
        try:
            await cog.apply_verified_roles(after, link.platform, leagues)
        except discord.HTTPException:
            logger.exception("Could not restore cached verification roles for %s", after.id)
        return

    cog._managed_role_updates.add(after.id)
    try:
        to_remove = after_roles - before_roles
        to_add = before_roles - after_roles
        if to_remove:
            await after.remove_roles(*to_remove, reason="Ochrona ról weryfikacji Riot")
        if to_add:
            await after.add_roles(*to_add, reason="Ochrona ról weryfikacji Riot")
    except discord.HTTPException:
        logger.exception("Could not restore previous verification roles for %s", after.id)
    finally:
        cog._managed_role_updates.discard(after.id)
    await cog.bot.verifications.schedule_rank_refresh_now(after.guild.id, after.id)


class VerificationStartView(discord.ui.View):
    def __init__(self, bot: MoonPoroBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.cooldowns: commands.CooldownMapping[discord.Interaction] = (
            commands.CooldownMapping.from_cooldown(
                1.0,
                bot.settings.verification_cooldown,
                _interaction_user_id,
            )
        )

    @discord.ui.button(
        label="🔐 Zweryfikuj przez Riot",
        style=discord.ButtonStyle.red,
        custom_id="verification:start:rso:v1",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[VerificationStartView],
    ) -> None:
        retry_after = self.cooldowns.update_rate_limit(interaction)
        if retry_after:
            await interaction.response.send_message(
                f"Spróbuj ponownie za {int(retry_after)} s.", ephemeral=True
            )
            return
        if isinstance(interaction.user, discord.Member) and member_has_role(
            interaction.user, self.bot.settings.verified_role_name, self.bot.settings
        ):
            await interaction.response.send_message(
                "Jesteś już zweryfikowany. Użyj `/usun_weryfikacje`, aby usunąć powiązanie.",
                ephemeral=True,
            )
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Weryfikację rozpocznij na serwerze Discord.", ephemeral=True
            )
            return
        if await self.bot.verifications.get_by_user(interaction.guild.id, interaction.user.id):
            await interaction.response.send_message(
                "To konto Discord ma już zapisane powiązanie. Użyj `/usun_weryfikacje`, "
                "jeśli chcesz połączyć inne konto Riot.",
                ephemeral=True,
            )
            return

        session = await self.bot.verification_sessions.create(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            ttl_seconds=self.bot.settings.rso_session_ttl_seconds,
        )
        verification_url = f"{self.bot.settings.rso_base_url}/verify/start/{session.token}"
        link_view = discord.ui.View(timeout=self.bot.settings.rso_session_ttl_seconds)
        link_view.add_item(
            discord.ui.Button(
                label="Przejdź do bezpiecznego logowania Riot",
                style=discord.ButtonStyle.link,
                url=verification_url,
            )
        )
        minutes = self.bot.settings.rso_session_ttl_seconds // 60
        embed = discord.Embed(
            title="Połącz konto przez Riot Sign On",
            description=(
                "Kliknij przycisk poniżej i zaloguj się bezpośrednio na stronie Riot. "
                "Moon Poro nie widzi Twojego hasła i nie zapisuje tokenów logowania. "
                f"Jednorazowy link wygaśnie za {minutes} min."
            ),
            colour=discord.Colour.from_rgb(116, 211, 224),
        )
        embed.set_footer(
            text="Po zakończeniu wróć do Discorda — role zostaną nadane automatycznie."
        )
        await interaction.response.send_message(embed=embed, view=link_view, ephemeral=True)


class VerificationCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self._managed_role_updates: set[int] = set()
        bot.add_view(VerificationStartView(bot))
        self.refresh_verified.change_interval(
            seconds=bot.settings.rank_refresh_worker_interval_seconds
        )
        self.complete_rso_verifications.change_interval(
            seconds=bot.settings.rso_completion_interval_seconds
        )
        self.refresh_verified.start()
        self.verification_maintenance.start()
        self.complete_rso_verifications.start()

    async def cog_unload(self) -> None:
        self.refresh_verified.cancel()
        self.verification_maintenance.cancel()
        self.complete_rso_verifications.cancel()

    async def apply_verified_roles(
        self,
        member: discord.Member,
        platform: str,
        leagues: LeagueEntries,
    ) -> None:
        self._managed_role_updates.add(member.id)
        try:
            await _apply_verified_roles(self.bot, member, platform, leagues)
        finally:
            self._managed_role_updates.discard(member.id)

    @tasks.loop(seconds=3, reconnect=True)
    async def complete_rso_verifications(self) -> None:
        records = await self.bot.verification_sessions.claim_pending(limit=5)
        for record in records:
            try:
                await self._complete_rso_verification(record)
            except Exception:
                logger.exception("Unexpected RSO completion error for session %s", record.id)
                await self._retry_rso_completion(record, "UNEXPECTED_COMPLETION_ERROR")

    async def _complete_rso_verification(self, record: VerificationSession) -> None:
        if record.platform is None or record.puuid is None:
            await self.bot.verification_sessions.fail_discord(record.id, "INCOMPLETE_RSO_DATA")
            return
        guild = self.bot.get_guild(record.guild_id)
        if guild is None:
            await self.bot.verification_sessions.fail_discord(record.id, "GUILD_NOT_FOUND")
            return
        member = guild.get_member(record.discord_user_id)
        if member is None:
            try:
                member = await guild.fetch_member(record.discord_user_id)
            except discord.NotFound:
                await self.bot.verification_sessions.fail_discord(record.id, "MEMBER_LEFT_GUILD")
                return
            except discord.HTTPException:
                await self._retry_rso_completion(record, "DISCORD_MEMBER_UNAVAILABLE")
                return

        try:
            leagues = await _get_leagues(self.bot, record.platform, record.puuid)
            await self.apply_verified_roles(member, record.platform, leagues)
        except RiotAPIUnavailable:
            await self._retry_rso_completion(record, "RIOT_API_UNAVAILABLE")
            return
        except discord.HTTPException:
            await self._retry_rso_completion(record, "DISCORD_ROLES_UNAVAILABLE")
            return

        await self.bot.verifications.complete_rank_refresh(
            record.guild_id,
            record.discord_user_id,
            rank_tier=get_rank_from_leagues(leagues).upper(),
            refresh_interval_hours=self.bot.settings.rank_refresh_interval_hours,
        )

        audit_message_id: int | None = None
        channel_id = self.bot.settings.zweryfikowani_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.abc.Messageable):
            embed = discord.Embed(title="Zweryfikowane konto — RSO", colour=discord.Colour.green())
            embed.add_field(name="Discord", value=f"<@{member.id}> (`{member.id}`)")
            embed.add_field(name="Region", value=SERVER_TRANSLATION[record.platform])
            if record.riot_game_name and record.riot_tag_line:
                embed.add_field(
                    name="Riot ID",
                    value=f"{record.riot_game_name}#{record.riot_tag_line}",
                    inline=False,
                )
            try:
                audit_message = await channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                audit_message_id = audit_message.id
            except discord.HTTPException:
                logger.exception("Could not publish RSO audit message for session %s", record.id)
        else:
            logger.error("Verification audit channel %s is unavailable", channel_id)

        completed = await self.bot.verification_sessions.complete_discord(
            record.id, message_id=audit_message_id
        )
        if not completed:
            await _remove_verified_roles(
                self.bot,
                member,
                reason="Weryfikacja RSO została anulowana w trakcie finalizacji",
            )
            if audit_message_id is not None and isinstance(channel, discord.abc.Messageable):
                with suppress(discord.NotFound, discord.HTTPException):
                    await channel.get_partial_message(audit_message_id).delete()
            return
        with suppress(discord.Forbidden, discord.HTTPException):
            await member.send(
                "Twoje konto Riot zostało zweryfikowane przez Riot Sign On. "
                f"Na serwerze **{guild.name}** zaktualizowano role regionu i rangi."
            )

    async def _retry_rso_completion(self, record: VerificationSession, error_code: str) -> None:
        delay = min(5 * (2 ** max(record.completion_attempts - 1, 0)), 300)
        await self.bot.verification_sessions.retry_discord(
            record.id,
            error_code=error_code,
            delay_seconds=delay,
        )

    @tasks.loop(seconds=10, reconnect=True)
    async def refresh_verified(self) -> None:
        try:
            await _refresh_next_verified(self)
        except Exception:
            logger.exception("Unexpected rank refresh worker error; next run will retry")

    @tasks.loop(hours=24, reconnect=True)
    async def verification_maintenance(self) -> None:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            return
        try:
            removed_logs = await self.bot.verifications.purge_access_logs(
                guild.id, self.bot.settings.verification_access_log_retention_days
            )
            (
                expired_sessions,
                purged_sessions,
            ) = await self.bot.verification_sessions.expire_and_purge(
                retention_days=self.bot.settings.verification_session_retention_days
            )
        except Exception:
            logger.exception("Could not run verification maintenance; next run will retry")
            return
        if removed_logs:
            logger.info("Removed %s expired verification access logs", removed_logs)
        if expired_sessions or purged_sessions:
            logger.info("Expired %s and purged %s RSO sessions", expired_sessions, purged_sessions)

    @refresh_verified.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    @verification_maintenance.before_loop
    async def before_maintenance(self) -> None:
        await self.bot.wait_until_ready()

    @complete_rso_verifications.before_loop
    async def before_rso_completion(self) -> None:
        await self.bot.wait_until_ready()

    @refresh_verified.error
    async def refresh_error(self, error: BaseException) -> None:
        logger.exception("Rank refresh loop failed", exc_info=error)

    @verification_maintenance.error
    async def maintenance_error(self, error: BaseException) -> None:
        logger.exception("Verification maintenance loop failed", exc_info=error)

    @complete_rso_verifications.error
    async def rso_completion_error(self, error: BaseException) -> None:
        logger.exception("RSO completion loop failed", exc_info=error)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await _restore_cached_roles_on_join(self, member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        await _reconcile_cached_role_change(self, before, after)

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="weryfikacja", description="Publikuje panel weryfikacji Riot")
    async def publish_verification(self, interaction: discord.Interaction) -> None:
        privacy_url = self.bot.settings.privacy_policy_url or (
            f"{self.bot.settings.rso_base_url}/privacy"
        )
        privacy_reference = f"Polityka prywatności: {privacy_url}."
        content = (
            "**Weryfikacja konta przez Riot Sign On**\n"
            "Po kliknięciu otrzymasz prywatny, jednorazowy link do oficjalnego logowania Riot. "
            "Bot nie widzi hasła i nie zapisuje tokenów logowania. Zapisujemy PUUID, region i "
            "powiązanie z Discordem, aby nadawać oraz aktualizować rangę. Dane nie są publiczne. "
            "Uprawnieni administratorzy mogą wykonać audytowany lookup wyłącznie dla moderacji "
            f"lub pomocy technicznej. {privacy_reference} "
            f"Warunki korzystania: {self.bot.settings.rso_base_url}/terms"
        )
        await interaction.response.send_message(content, view=VerificationStartView(self.bot))

    @app_commands.command(
        name="usun_weryfikacje", description="Usuwa Twoje powiązanie z kontem Riot"
    )
    async def remove_own_verification(self, interaction: discord.Interaction) -> None:
        link = await self.bot.verifications.delete_by_user(
            interaction.guild_id or 0, interaction.user.id
        )
        await self.bot.verification_sessions.cancel_for_user(
            interaction.guild_id or 0, interaction.user.id
        )
        if interaction.guild is not None:
            if isinstance(interaction.user, discord.Member):
                await _remove_verified_roles(
                    self.bot,
                    interaction.user,
                    reason="Usunięcie weryfikacji przez użytkownika",
                )
            if link and link.message_id and self.bot.settings.zweryfikowani_channel_id:
                channel = interaction.guild.get_channel(self.bot.settings.zweryfikowani_channel_id)
                if isinstance(channel, discord.abc.Messageable):
                    with suppress(discord.NotFound):
                        await channel.get_partial_message(link.message_id).delete()
        await interaction.response.send_message("Usunięto powiązanie konta Riot.", ephemeral=True)

    async def _account_by_riot_id(self, nick: str, tag: str, platform: str) -> RiotPayload | None:
        return await riot_api_call(
            lambda: self.bot.riot_client.get_account_v1_by_riot_id(
                game_name=nick.strip(),
                tag_line=tag.replace("#", "").strip(),
                region=API_SERVERS[platform],
            ),
            not_found=None,
        )

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(server=_server_choices(), powod=_lookup_reason_choices())
    @app_commands.command(name="show_wer_user", description="Znajduje Discord na podstawie Riot ID")
    async def lookup_by_riot_id(
        self,
        interaction: discord.Interaction,
        nick: str,
        tag: str,
        server: str,
        powod: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            account = await self._account_by_riot_id(nick, tag, server)
        except RiotAPIUnavailable:
            await interaction.followup.send("Riot API jest chwilowo niedostępne.", ephemeral=True)
            return
        link = (
            await self.bot.verifications.get_by_puuid(interaction.guild_id or 0, account["puuid"])
            if account
            else None
        )
        await self.bot.verifications.log_access(
            guild_id=interaction.guild_id or 0,
            actor_id=interaction.user.id,
            reason=powod,
            discord_user_id=link.discord_user_id if link else None,
            puuid=account["puuid"] if account else None,
        )
        if link:
            await interaction.followup.send(
                f"Powiązane konto Discord: <@{link.discord_user_id}> (`{link.discord_user_id}`). "
                "Lookup zapisano w dzienniku audytowym.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                "Nie znaleziono powiązania. Lookup zapisano.", ephemeral=True
            )

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(
        name="show_wer_discord", description="Pokazuje Riot ID przypisane do konta Discord"
    )
    @app_commands.choices(powod=_lookup_reason_choices())
    async def lookup_by_discord(
        self,
        interaction: discord.Interaction,
        uzytkownik: discord.Member,
        powod: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        link = await self.bot.verifications.get_by_user(interaction.guild_id or 0, uzytkownik.id)
        await self.bot.verifications.log_access(
            guild_id=interaction.guild_id or 0,
            actor_id=interaction.user.id,
            reason=powod,
            discord_user_id=uzytkownik.id,
            puuid=link.puuid if link else None,
        )
        if link is None or not link.puuid:
            await interaction.followup.send(
                "Nie znaleziono powiązania. Lookup zapisano.", ephemeral=True
            )
            return
        try:
            account = await riot_api_call(
                lambda: self.bot.riot_client.get_account_v1_by_puuid(
                    puuid=link.puuid, region=API_SERVERS[link.platform]
                )
            )
        except RiotAPIUnavailable:
            await interaction.followup.send("Riot API jest chwilowo niedostępne.", ephemeral=True)
            return
        if account is None:
            await interaction.followup.send("Nie udało się pobrać Riot ID.", ephemeral=True)
            return
        await interaction.followup.send(
            f"Konto Riot: **{account['gameName']}#{account['tagLine']}**, region "
            f"**{SERVER_TRANSLATION[link.platform]}**. Lookup zapisano.",
            ephemeral=True,
        )

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(server=_server_choices())
    @app_commands.command(
        name="usun_wer_nick", description="Administracyjnie usuwa powiązanie Riot ID"
    )
    async def remove_by_riot_id(
        self,
        interaction: discord.Interaction,
        nick: str,
        tag: str,
        server: str,
        powod: app_commands.Range[str, 5, 300],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            account = await self._account_by_riot_id(nick, tag, server)
        except RiotAPIUnavailable:
            await interaction.followup.send("Riot API jest chwilowo niedostępne.", ephemeral=True)
            return
        if account is None:
            await interaction.followup.send("Nie znaleziono Riot ID.", ephemeral=True)
            return
        link = await self.bot.verifications.get_by_puuid(
            interaction.guild_id or 0, account["puuid"]
        )
        await self.bot.verifications.log_access(
            guild_id=interaction.guild_id or 0,
            actor_id=interaction.user.id,
            reason=f"Usunięcie powiązania: {powod}",
            discord_user_id=link.discord_user_id if link else None,
            puuid=account["puuid"],
        )
        removed = await self.bot.verifications.delete_by_puuid(
            interaction.guild_id or 0, account["puuid"]
        )
        if removed is None:
            await interaction.followup.send("To konto nie było powiązane.", ephemeral=True)
            return
        await self.bot.verification_sessions.cancel_for_user(
            interaction.guild_id or 0, removed.discord_user_id
        )
        if interaction.guild is not None:
            member = interaction.guild.get_member(removed.discord_user_id)
            if member:
                await _remove_verified_roles(
                    self.bot, member, reason=f"Administracyjne usunięcie: {powod}"
                )
            channel_id = self.bot.settings.zweryfikowani_channel_id
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            if removed.message_id and isinstance(channel, discord.abc.Messageable):
                with suppress(discord.NotFound):
                    await channel.get_partial_message(removed.message_id).delete()
        await interaction.followup.send("Usunięto powiązanie i zapisano operację.", ephemeral=True)


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(VerificationCog(bot))
