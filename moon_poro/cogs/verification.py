from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from moon_poro.account_profile import (
    REFRESH_RUNNING_BUTTON_LABEL,
    AccountProfilePresentation,
    AccountProfileState,
    build_account_profile,
    set_account_profile_status,
)
from moon_poro.bot import MoonPoroBot
from moon_poro.models import VerificationLink, VerificationSession
from moon_poro.permissions import administrator_only
from moon_poro.rank_refresh import (
    RankSnapshot,
    decide_rank_refresh,
    effective_refresh_interval,
    solo_rank_snapshot,
)
from moon_poro.repositories import RankRefreshRequestStatus
from moon_poro.riot import (
    API_SERVERS,
    SERVER_TRANSLATION,
    RiotAPINotFound,
    RiotAPIUnavailable,
    get_discord_rank_role,
    get_rank_from_leagues,
    riot_api_call,
)
from moon_poro.roles import find_role, member_has_role, member_roles_named

logger = logging.getLogger("moon_poro.verification")

type RiotPayload = dict[str, Any]
type LeagueEntries = list[RiotPayload]
type VerificationStarter = Callable[[discord.Interaction], Awaitable[None]]

_ACCOUNT_PROFILE_REFRESH_WATCH_TIMEOUT_SECONDS = 60.0
_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS = 2.0
_ACCOUNT_PROFILE_VIEW_TIMEOUT_BUFFER_SECONDS = 5 * 60
_ACCOUNT_PROFILE_PENDING_STATES = frozenset(
    {AccountProfileState.REFRESH_QUEUED, AccountProfileState.REFRESH_RUNNING}
)
_ACCOUNT_PROFILE_OBSERVABLE_REQUESTS = frozenset(
    {
        RankRefreshRequestStatus.ENQUEUED,
        RankRefreshRequestStatus.ALREADY_DUE,
        RankRefreshRequestStatus.ALREADY_CLAIMED,
    }
)
_ACCOUNT_PROFILE_STALE_MESSAGE = "Ten widok jest już nieaktualny. Otwórz `/profil` ponownie."
_ACCOUNT_PROFILE_REFRESH_TIMEOUT_MESSAGE = (
    "Odświeżanie trwa dłużej niż zwykle.\nOtwórz `/profil` ponownie za chwilę."
)
_ACCOUNT_PROFILE_REFRESH_OBSERVATION_ERROR_MESSAGE = (
    "Nie udało się pokazać wyniku odświeżania.\n"
    "Odświeżanie może nadal trwać.\n"
    "Otwórz `/profil` ponownie za chwilę."
)


def _rank_refresh_cooldown_message(retry_after_seconds: int | None) -> str:
    if retry_after_seconds is None or retry_after_seconds <= 0:
        return "Rangę będzie można odświeżyć za chwilę."
    seconds = retry_after_seconds
    if seconds >= 60:
        minutes = math.ceil(seconds / 60)
        return f"Rangę możesz odświeżyć za około {minutes} min."
    return f"Rangę możesz odświeżyć za {seconds} s."


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
    return await riot_api_call(
        lambda: bot.riot_client.get_lol_league_v4_entries_by_puuid(
            region=platform,
            puuid=puuid,
        )
    )


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


async def _remove_verified_marker(bot: MoonPoroBot, member: discord.Member, *, reason: str) -> None:
    verified_role = find_role(member.guild, bot.settings.verified_role_name, bot.settings)
    if verified_role is not None and verified_role in member.roles:
        await member.remove_roles(verified_role, reason=reason)


def _cached_rank_leagues(link: VerificationLink) -> LeagueEntries | None:
    if link.last_known_rank is None:
        return None
    return [
        {
            "queueType": "RANKED_SOLO_5x5",
            "tier": link.last_known_rank,
            "rank": getattr(link, "last_known_division", None),
            "leaguePoints": getattr(link, "last_known_league_points", None),
            "wins": getattr(link, "last_known_wins", None),
            "losses": getattr(link, "last_known_losses", None),
            "inactive": getattr(link, "last_known_inactive", None),
        }
    ]


def _snapshot_from_link(link: VerificationLink) -> RankSnapshot | None:
    if link.last_known_rank is None:
        return None
    return RankSnapshot(
        tier=link.last_known_rank,
        division=getattr(link, "last_known_division", None),
        league_points=getattr(link, "last_known_league_points", None),
        wins=getattr(link, "last_known_wins", None),
        losses=getattr(link, "last_known_losses", None),
        inactive=getattr(link, "last_known_inactive", None),
    )


async def _record_leagues(
    bot: MoonPoroBot,
    link: VerificationLink,
    leagues: LeagueEntries,
) -> tuple[LeagueEntries, datetime] | None:
    decision = decide_rank_refresh(
        _snapshot_from_link(link),
        solo_rank_snapshot(leagues),
        previous_unranked_confirmations=getattr(link, "rank_unranked_confirmations", 0),
    )
    interval = effective_refresh_interval(
        decision,
        policy=bot.settings.rank_refresh_policy,
        guild_id=link.guild_id,
        user_id=link.discord_user_id,
        rollout_percent=bot.settings.rank_refresh_rollout_percent,
        fixed_interval_seconds=bot.settings.rank_refresh_interval_hours * 3600,
    )
    checked_at = await bot.verifications.record_rank_snapshot(
        link.guild_id,
        link.discord_user_id,
        expected_puuid=link.puuid or "",
        expected_platform=link.platform,
        expected_created_at=link.created_at,
        decision=decision,
        next_interval_seconds=interval,
    )
    if checked_at is None:
        return None
    return (
        [
            {
                "queueType": "RANKED_SOLO_5x5",
                "tier": decision.snapshot.tier,
                "rank": decision.snapshot.division,
                "leaguePoints": decision.snapshot.league_points,
                "wins": decision.snapshot.wins,
                "losses": decision.snapshot.losses,
                "inactive": decision.snapshot.inactive,
            }
        ],
        checked_at,
    )


async def _reconcile_applied_roles(
    cog: VerificationCog,
    member: discord.Member,
    applied_link: VerificationLink,
) -> bool:
    """Confirm a role side effect still belongs to the current link.

    Discord operations cannot participate in the database transaction. Re-read the
    link after applying roles so a concurrent delete or re-verification cannot leave
    an obsolete Verified marker behind.
    """

    current = await cog.bot.verifications.get_by_user(member.guild.id, member.id)
    identity_matches = (
        current is not None
        and getattr(current, "deletion_requested_at", None) is None
        and current.puuid == applied_link.puuid
        and current.platform == applied_link.platform
        and getattr(current, "created_at", None) == getattr(applied_link, "created_at", None)
    )
    if identity_matches:
        return True
    await _remove_verified_marker_durably(
        cog,
        member,
        reason="Anulowanie nieaktualnej synchronizacji weryfikacji Riot",
        observed_current=current,
    )
    return False


async def _ensure_active_link_roles(
    cog: VerificationCog,
    member: discord.Member,
    link: VerificationLink,
) -> bool:
    cached = _cached_rank_leagues(link)
    if cached is None:
        await cog.bot.verifications.schedule_rank_refresh_now(member.guild.id, member.id)
    else:
        try:
            await cog.apply_verified_roles(member, link.platform, cached)
        except discord.HTTPException:
            await cog.bot.verifications.retry_rank_role_sync(
                member.guild.id,
                member.id,
                base_delay_seconds=cog.bot.settings.rank_refresh_retry_base_seconds,
                expected_rank_last_checked_at=link.rank_last_checked_at,
                expected_puuid=link.puuid,
                expected_platform=link.platform,
                expected_created_at=link.created_at,
            )
            logger.exception(
                "Could not restore roles after obsolete marker cleanup for %s", member.id
            )
        else:
            current = await cog.bot.verifications.get_by_user(member.guild.id, member.id)
            identity_matches = (
                current is not None
                and current.deletion_requested_at is None
                and current.puuid == link.puuid
                and current.platform == link.platform
                and current.created_at == link.created_at
            )
            if identity_matches:
                await cog.bot.verifications.acknowledge_rank_role_sync(
                    member.guild.id,
                    member.id,
                    expected_rank_last_checked_at=link.rank_last_checked_at,
                    expected_puuid=link.puuid,
                    expected_platform=link.platform,
                    expected_created_at=link.created_at,
                )
                return True
            return False
    current = await cog.bot.verifications.get_by_user(member.guild.id, member.id)
    return (
        current is not None
        and current.deletion_requested_at is None
        and current.puuid == link.puuid
        and current.platform == link.platform
        and current.created_at == link.created_at
    )


async def _remove_verified_marker_durably(
    cog: VerificationCog,
    member: discord.Member,
    *,
    reason: str,
    observed_current: VerificationLink | None = None,
    already_enqueued: bool = False,
    expected_generation: int | None = None,
) -> None:
    bot = cog.bot
    guild_id = member.guild.id
    if not already_enqueued:
        expected_generation = await bot.verifications.enqueue_verified_marker_cleanup(
            guild_id, member.id
        )
    if expected_generation is None:
        raise RuntimeError("Marker cleanup generation is required for an existing outbox claim")
    current = observed_current
    if current is None and already_enqueued:
        current = await bot.verifications.get_by_user(guild_id, member.id)
    for _attempt in range(4):
        if current is not None and current.deletion_requested_at is None:
            if await _ensure_active_link_roles(cog, member, current):
                await bot.verifications.acknowledge_verified_marker_cleanup(
                    guild_id,
                    member.id,
                    expected_generation=expected_generation,
                )
                return
            current = await bot.verifications.get_by_user(guild_id, member.id)
            continue
        try:
            await _remove_verified_marker(bot, member, reason=reason)
        except discord.HTTPException:
            delay = await bot.verifications.retry_verified_marker_cleanup(
                guild_id,
                member.id,
                expected_generation=expected_generation,
                base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            )
            logger.exception(
                "Could not remove obsolete Verified marker for %s; retrying in %s seconds",
                member.id,
                delay,
            )
            return
        current = await bot.verifications.get_by_user(guild_id, member.id)
        if current is None or current.deletion_requested_at is not None:
            await bot.verifications.acknowledge_verified_marker_cleanup(
                guild_id,
                member.id,
                expected_generation=expected_generation,
            )
            return
    await bot.verifications.retry_verified_marker_cleanup(
        guild_id,
        member.id,
        expected_generation=expected_generation,
        base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
    )


async def _retry_next_verified_marker_cleanup(cog: VerificationCog) -> None:
    bot = cog.bot
    guild = bot.get_guild(bot.settings.guild_id)
    if guild is None:
        return
    records = await bot.verifications.claim_due_verified_marker_cleanups(
        guild.id,
        limit=1,
        claim_timeout_seconds=bot.settings.rank_refresh_claim_timeout_seconds,
    )
    if not records:
        return
    record = records[0]
    member = guild.get_member(record.discord_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(record.discord_user_id)
        except discord.NotFound:
            await bot.verifications.acknowledge_verified_marker_cleanup(
                guild.id,
                record.discord_user_id,
                expected_generation=record.generation,
            )
            return
        except discord.HTTPException:
            await bot.verifications.retry_verified_marker_cleanup(
                guild.id,
                record.discord_user_id,
                expected_generation=record.generation,
                base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            )
            return
    await _remove_verified_marker_durably(
        cog,
        member,
        reason="Dokończenie usuwania nieaktualnej roli weryfikacji Riot",
        already_enqueued=True,
        expected_generation=record.generation,
    )


async def _retry_next_rank_role_sync(cog: VerificationCog) -> None:
    bot = cog.bot
    guild = bot.get_guild(bot.settings.guild_id)
    if guild is None:
        return
    links = await bot.verifications.claim_due_rank_role_syncs(
        guild.id,
        limit=1,
        claim_timeout_seconds=bot.settings.rank_refresh_claim_timeout_seconds,
    )
    if not links:
        return
    link = links[0]
    member = guild.get_member(link.discord_user_id)
    if member is None:
        await bot.verifications.defer_rank_role_sync(
            guild.id,
            link.discord_user_id,
            delay_seconds=7 * 86_400,
            expected_rank_last_checked_at=link.rank_last_checked_at,
            expected_puuid=link.puuid or "",
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        return
    leagues = _cached_rank_leagues(link)
    if leagues is None:
        acknowledged = await bot.verifications.acknowledge_rank_role_sync(
            guild.id,
            link.discord_user_id,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        if acknowledged:
            await bot.verifications.schedule_rank_refresh_now(guild.id, link.discord_user_id)
        return
    try:
        await cog.apply_verified_roles(member, link.platform, leagues)
    except discord.HTTPException:
        delay = await bot.verifications.retry_rank_role_sync(
            guild.id,
            link.discord_user_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            expected_rank_last_checked_at=link.rank_last_checked_at,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        logger.exception(
            "Could not synchronize cached Discord rank roles for user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return
    if not await _reconcile_applied_roles(cog, member, link):
        return
    await bot.verifications.acknowledge_rank_role_sync(
        guild.id,
        link.discord_user_id,
        expected_rank_last_checked_at=link.rank_last_checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )


async def _retry_next_rso_audit_cleanup(cog: VerificationCog) -> None:
    """Delete an RSO audit message that lost a race with cancellation."""

    bot = cog.bot
    records = await bot.verification_sessions.claim_audit_cleanups(
        limit=1,
        claim_timeout_seconds=bot.settings.rank_refresh_claim_timeout_seconds,
    )
    if not records:
        return
    record = records[0]
    guild = bot.get_guild(record.guild_id)
    channel = guild.get_channel(record.channel_id) if guild is not None else None
    if not isinstance(channel, discord.abc.Messageable):
        await bot.verification_sessions.retry_audit_cleanup(
            record.id,
            message_id=record.message_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
        )
        return
    try:
        await channel.get_partial_message(record.message_id).delete()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        delay = await bot.verification_sessions.retry_audit_cleanup(
            record.id,
            message_id=record.message_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
        )
        logger.exception(
            "Could not remove orphaned RSO audit message %s; retrying in %s seconds",
            record.message_id,
            delay,
        )
        return
    await bot.verification_sessions.acknowledge_audit_cleanup(
        record.id,
        message_id=record.message_id,
    )


async def _refresh_next_verified(cog: VerificationCog) -> None:
    bot = cog.bot
    if not bot.riot_auth_breaker.can_attempt():
        return
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
    if link.puuid is None:
        return
    member = guild.get_member(link.discord_user_id)
    if member is None:
        await bot.verifications.defer_rank_refresh(
            guild.id,
            link.discord_user_id,
            delay_seconds=7 * 86_400,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        return

    try:
        leagues = await _get_leagues(bot, link.platform, link.puuid)
    except RiotAPINotFound:
        delay = await bot.verifications.retry_rank_refresh(
            guild.id,
            link.discord_user_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        logger.warning(
            "Riot League-v4 returned 404 for Discord user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return
    except RiotAPIUnavailable as error:
        if error.status in {401, 403} or not bot.riot_auth_breaker.can_attempt():
            await bot.verifications.release_rank_refresh_claim(
                guild.id,
                link.discord_user_id,
                expected_puuid=link.puuid,
                expected_platform=link.platform,
                expected_created_at=link.created_at,
            )
            logger.error(
                "Paused automatic Riot rank refreshes after authentication failure HTTP %s",
                error.status,
            )
            return
        delay = await bot.verifications.retry_rank_refresh(
            guild.id,
            link.discord_user_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        logger.warning(
            "Could not refresh Riot rank for Discord user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return

    recorded = await _record_leagues(bot, link, leagues)
    if recorded is None:
        return
    cached_leagues, checked_at = recorded
    try:
        await cog.apply_verified_roles(member, link.platform, cached_leagues)
    except discord.HTTPException:
        await bot.verifications.retry_rank_role_sync(
            guild.id,
            link.discord_user_id,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            expected_rank_last_checked_at=checked_at,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )
        logger.exception("Riot snapshot stored but Discord role sync failed for user %s", member.id)
        return
    if not await _reconcile_applied_roles(cog, member, link):
        return
    await bot.verifications.acknowledge_rank_role_sync(
        guild.id,
        link.discord_user_id,
        expected_rank_last_checked_at=checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )


def _utc_timestamp(value: datetime | None, *, missing: str) -> str:
    if value is None:
        return missing
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


async def _report_riot_monitoring(
    bot: MoonPoroBot,
    *,
    now: datetime | None = None,
) -> None:
    metrics = bot.riot_monitor.snapshot()
    auth = bot.riot_auth_breaker.snapshot()
    queue_due: int | str = "unknown"
    oldest_due_at: datetime | None = None
    oldest_overdue_seconds: int | str = "unknown"
    queue_details = "rank_refresh_schedule_stats=unknown"
    try:
        queue = await bot.verifications.rank_refresh_queue_stats(bot.settings.guild_id)
    except Exception:
        logger.exception("Could not read the rank refresh queue for Riot monitoring")
    else:
        queue_due = queue.due_count
        oldest_due_at = queue.oldest_due_at
        if oldest_due_at is None:
            oldest_overdue_seconds = 0
        else:
            current = now or datetime.now(UTC)
            if oldest_due_at.tzinfo is None:
                oldest_due_at = oldest_due_at.replace(tzinfo=UTC)
            oldest_overdue_seconds = max(0, int((current - oldest_due_at).total_seconds()))
        current = now or datetime.now(UTC)
        queue_details = (
            f"rank_refresh_schedule_6h={queue.schedule_6h_count} "
            f"rank_refresh_schedule_12h={queue.schedule_12h_count} "
            f"rank_refresh_schedule_24h={queue.schedule_24h_count} "
            f"rank_refresh_predicted_requests_per_day={queue.predicted_requests_per_day:.1f} "
            f"rank_snapshot_age_p50_seconds={_age_seconds(current, queue.snapshot_p50_at)} "
            f"rank_snapshot_age_p95_seconds={_age_seconds(current, queue.snapshot_p95_at)} "
            f"rank_snapshot_age_max_seconds={_age_seconds(current, queue.oldest_snapshot_at)} "
            f"rank_tier_changes_total={queue.tier_changes} "
            f"rank_counter_resets_total={queue.counter_resets} "
            f"rank_unranked_confirmations_pending={queue.unranked_confirmations} "
            f"rank_role_sync_pending={queue.pending_role_sync_count}"
        )

    logger.info(
        "Riot monitoring: responses_429_since_start=%s responses_401_since_start=%s "
        "responses_403_since_start=%s responses_5xx_since_start=%s "
        "rank_refresh_queue_due=%s rank_refresh_oldest_due_at_utc=%s "
        "rank_refresh_oldest_overdue_seconds=%s last_successful_riot_response_utc=%s "
        "riot_auth_breaker_open=%s riot_auth_breaker_status=%s "
        "riot_auth_probe_retry_after_seconds=%.1f %s",
        metrics.responses_429,
        metrics.responses_401,
        metrics.responses_403,
        metrics.responses_5xx,
        queue_due,
        _utc_timestamp(oldest_due_at, missing="none"),
        oldest_overdue_seconds,
        _utc_timestamp(metrics.last_success_at, missing="never"),
        auth.blocked,
        auth.last_status,
        auth.retry_after_seconds,
        queue_details,
    )


def _age_seconds(now: datetime, value: datetime | None) -> int | str:
    if value is None:
        return "none"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((now - value).total_seconds()))


async def _restore_cached_roles_on_join(cog: VerificationCog, member: discord.Member) -> None:
    if member.guild.id != cog.bot.settings.guild_id:
        return
    link = await cog.bot.verifications.get_by_user(member.guild.id, member.id)
    if link is None or not link.puuid:
        return
    if getattr(link, "deletion_requested_at", None) is not None:
        try:
            await _remove_verified_marker(
                cog.bot,
                member,
                reason="Dokończenie usuwania weryfikacji Riot",
            )
        except discord.HTTPException:
            logger.exception("Could not enforce pending verification deletion for %s", member.id)
        return
    leagues = _cached_rank_leagues(link)
    if leagues is None:
        await cog.bot.verifications.request_rank_refresh(
            member.guild.id,
            member.id,
            cooldown_seconds=cog.bot.settings.rank_refresh_manual_priority_cooldown_seconds,
            source="role_tamper",
        )
        return
    try:
        await cog.apply_verified_roles(member, link.platform, leagues)
    except discord.HTTPException:
        logger.exception("Could not restore cached verification roles for %s", member.id)
    else:
        if not await _reconcile_applied_roles(cog, member, link):
            return
        if getattr(link, "rank_role_sync_pending", False):
            await cog.bot.verifications.acknowledge_rank_role_sync(
                member.guild.id,
                member.id,
                expected_rank_last_checked_at=getattr(link, "rank_last_checked_at", None),
                expected_puuid=link.puuid,
                expected_platform=link.platform,
                expected_created_at=link.created_at,
            )
    checked_at = getattr(link, "rank_last_checked_at", None)
    if _timestamp_is_older_than(checked_at, timedelta(hours=24)):
        await cog.bot.verifications.schedule_rank_refresh_now(member.guild.id, member.id)


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
    if getattr(link, "deletion_requested_at", None) is not None:
        try:
            await _remove_verified_marker(
                cog.bot,
                after,
                reason="Dokończenie usuwania weryfikacji Riot",
            )
        except discord.HTTPException:
            logger.exception("Could not enforce pending verification deletion for %s", after.id)
        return
    leagues = _cached_rank_leagues(link)
    if leagues is not None:
        try:
            await cog.apply_verified_roles(after, link.platform, leagues)
        except discord.HTTPException:
            logger.exception("Could not restore cached verification roles for %s", after.id)
        else:
            if not await _reconcile_applied_roles(cog, after, link):
                return
            if getattr(link, "rank_role_sync_pending", False):
                await cog.bot.verifications.acknowledge_rank_role_sync(
                    after.guild.id,
                    after.id,
                    expected_rank_last_checked_at=getattr(link, "rank_last_checked_at", None),
                    expected_puuid=link.puuid,
                    expected_platform=link.platform,
                    expected_created_at=link.created_at,
                )
        checked_at = getattr(link, "rank_last_checked_at", None)
        if _timestamp_is_older_than(checked_at, timedelta(hours=1)):
            await cog.bot.verifications.request_rank_refresh(
                after.guild.id,
                after.id,
                cooldown_seconds=(cog.bot.settings.rank_refresh_manual_priority_cooldown_seconds),
                source="role_tamper",
            )
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
    if not await _reconcile_applied_roles(cog, after, link):
        return
    await cog.bot.verifications.request_rank_refresh(
        after.guild.id,
        after.id,
        cooldown_seconds=cog.bot.settings.rank_refresh_manual_priority_cooldown_seconds,
        source="role_tamper",
    )


def _timestamp_is_older_than(value: datetime | None, age: timedelta) -> bool:
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value < datetime.now(UTC) - age


async def _remove_user_verification(
    bot: MoonPoroBot,
    interaction: discord.Interaction,
    *,
    expected_puuid: str | None = None,
    expected_platform: str | None = None,
    expected_created_at: datetime | None = None,
) -> str:
    guild_id = interaction.guild_id or 0
    link = await bot.verifications.request_verification_deletion(
        guild_id,
        interaction.user.id,
        expected_puuid=expected_puuid,
        expected_platform=expected_platform,
        expected_created_at=expected_created_at,
    )
    if link is None:
        if any(
            value is not None for value in (expected_puuid, expected_platform, expected_created_at)
        ):
            return _ACCOUNT_PROFILE_STALE_MESSAGE
        return "Nie masz połączonego konta Riot."
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    completed = await _process_requested_verification_deletion(
        bot,
        guild=interaction.guild,
        member=member,
        link=link,
    )
    if not completed:
        return "Usuwanie powiązania jeszcze trwa. Spróbujemy dokończyć je automatycznie."
    return (
        "Usunięto powiązanie z kontem Riot oraz rolę „Zweryfikowany”. "
        "Role regionu, rangi i użytkownika pozostają bez zmian."
    )


async def _process_requested_verification_deletion(
    bot: MoonPoroBot,
    *,
    guild: discord.Guild | None,
    member: discord.Member | None,
    link: VerificationLink,
) -> bool:
    await bot.verification_sessions.cancel_for_user(link.guild_id, link.discord_user_id)
    if member is not None:
        try:
            await _remove_verified_marker(
                bot,
                member,
                reason="Usunięcie weryfikacji przez użytkownika",
            )
        except discord.HTTPException:
            delay = await bot.verifications.retry_verification_deletion(
                link.guild_id,
                link.discord_user_id,
                expected_created_at=link.created_at,
                base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            )
            logger.exception(
                "Could not remove Verified marker for user %s; deletion retry in %s seconds",
                link.discord_user_id,
                delay,
            )
            return False
    if guild is not None:
        channel_id = bot.settings.zweryfikowani_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if link.message_id and isinstance(channel, discord.abc.Messageable):
            try:
                await channel.get_partial_message(link.message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                delay = await bot.verifications.retry_verification_deletion(
                    link.guild_id,
                    link.discord_user_id,
                    expected_created_at=link.created_at,
                    base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
                )
                logger.exception(
                    "Could not remove verification audit for user %s; retrying in %s seconds",
                    link.discord_user_id,
                    delay,
                )
                return False
    return await bot.verifications.finalize_verification_deletion(
        link.guild_id,
        link.discord_user_id,
        expected_puuid=link.puuid or "",
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )


async def _retry_next_verification_deletion(cog: VerificationCog) -> None:
    bot = cog.bot
    guild = bot.get_guild(bot.settings.guild_id)
    if guild is None:
        return
    links = await bot.verifications.claim_due_verification_deletions(
        guild.id,
        limit=1,
        claim_timeout_seconds=bot.settings.rank_refresh_claim_timeout_seconds,
    )
    if not links:
        return
    link = links[0]
    member = guild.get_member(link.discord_user_id)
    if member is None:
        try:
            member = await guild.fetch_member(link.discord_user_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            delay = await bot.verifications.retry_verification_deletion(
                link.guild_id,
                link.discord_user_id,
                expected_created_at=link.created_at,
                base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            )
            logger.exception(
                "Could not resolve member %s for verification deletion; retrying in %s seconds",
                link.discord_user_id,
                delay,
            )
            return
    await _process_requested_verification_deletion(
        bot,
        guild=guild,
        member=member,
        link=link,
    )


async def _request_rank_refresh_from_panel(
    bot: MoonPoroBot,
    interaction: discord.Interaction,
) -> None:
    if interaction.guild_id != bot.settings.guild_id or not isinstance(
        interaction.user, discord.Member
    ):
        await interaction.response.send_message(
            "Rangę możesz odświeżyć tylko na serwerze Moon Poro.", ephemeral=True
        )
        return
    result = await bot.verifications.request_rank_refresh(
        interaction.guild_id,
        interaction.user.id,
        cooldown_seconds=bot.settings.rank_refresh_button_cooldown_seconds,
        source="user",
    )
    messages = {
        RankRefreshRequestStatus.ENQUEUED: "Odświeżenie rangi czeka w kolejce.",
        RankRefreshRequestStatus.ALREADY_DUE: "Odświeżenie rangi jest już w kolejce.",
        RankRefreshRequestStatus.ALREADY_CLAIMED: "Trwa już odświeżanie rangi.",
        RankRefreshRequestStatus.BACKOFF_ACTIVE: (
            "Riot jest chwilowo niedostępny. Spróbujemy ponownie automatycznie."
        ),
        RankRefreshRequestStatus.LINK_CHANGED: (
            "Dane konta zmieniły się. Otwórz `/profil`, aby zobaczyć aktualny stan."
        ),
        RankRefreshRequestStatus.NOT_LINKED: (
            "Najpierw kliknij „Zweryfikuj konto” i połącz konto Riot."
        ),
    }
    if result.status == RankRefreshRequestStatus.COOLDOWN:
        message = _rank_refresh_cooldown_message(result.retry_after_seconds)
    else:
        message = messages[result.status]
    await interaction.response.send_message(message, ephemeral=True)


def _riot_id_from_link(link: VerificationLink | None) -> str | None:
    if link is None:
        return None
    game_name = getattr(link, "riot_game_name", None)
    tag_line = getattr(link, "riot_tag_line", None)
    if not game_name or not tag_line:
        return None
    return f"{game_name}#{tag_line}"


def _build_account_profile(
    bot: MoonPoroBot,
    link: VerificationLink | None,
) -> AccountProfilePresentation:
    return build_account_profile(
        link,
        riot_id=_riot_id_from_link(link),
        manual_refresh_cooldown_seconds=(bot.settings.rank_refresh_button_cooldown_seconds),
        rank_refresh_claim_timeout_seconds=(bot.settings.rank_refresh_claim_timeout_seconds),
        riot_authorization_unavailable=bot.riot_auth_breaker.snapshot().blocked,
    )


def _same_timestamp(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    normalized_left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    normalized_right = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return normalized_left == normalized_right


class AccountProfileView(discord.ui.View):
    def __init__(
        self,
        bot: MoonPoroBot,
        *,
        owner_id: int,
        presentation: AccountProfilePresentation,
        link: VerificationLink | None,
        start_verification: VerificationStarter,
    ) -> None:
        super().__init__(
            timeout=(
                bot.settings.rank_refresh_button_cooldown_seconds
                + _ACCOUNT_PROFILE_VIEW_TIMEOUT_BUFFER_SECONDS
            )
        )
        self.bot = bot
        self.owner_id = owner_id
        self.presentation = presentation
        self.start_verification = start_verification
        self.expected_puuid = link.puuid if link is not None else None
        self.expected_platform = link.platform if link is not None else None
        self.expected_created_at = link.created_at if link is not None else None
        self._refresh_in_progress = False

        if presentation.state is AccountProfileState.UNVERIFIED:
            self.remove_item(self.refresh_rank)
            self.remove_item(self.remove_verification)
        elif presentation.state is AccountProfileState.DELETING:
            self.clear_items()
        else:
            self.remove_item(self.verify_account)
            self.refresh_rank.label = presentation.refresh_button_label
            self.refresh_rank.disabled = not presentation.refresh_enabled
            if presentation.state in _ACCOUNT_PROFILE_PENDING_STATES:
                self.refresh_rank.style = discord.ButtonStyle.secondary

    def _restore_refresh_button(
        self,
        button: discord.ui.Button[AccountProfileView],
    ) -> None:
        self._refresh_in_progress = False
        button.label = self.presentation.refresh_button_label
        button.disabled = not self.presentation.refresh_enabled
        button.style = discord.ButtonStyle.blurple

    def _matches_current_link(self, link: VerificationLink | None) -> bool:
        return bool(
            link is not None
            and link.deletion_requested_at is None
            and self.expected_puuid is not None
            and link.puuid == self.expected_puuid
            and self.expected_platform is not None
            and link.platform == self.expected_platform
            and self.expected_created_at is not None
            and _same_timestamp(link.created_at, self.expected_created_at)
        )

    def _new_view(
        self,
        presentation: AccountProfilePresentation,
        link: VerificationLink | None,
    ) -> AccountProfileView:
        return AccountProfileView(
            self.bot,
            owner_id=self.owner_id,
            presentation=presentation,
            link=link,
            start_verification=self.start_verification,
        )

    async def _edit_profile(
        self,
        interaction: discord.Interaction,
        link: VerificationLink,
    ) -> AccountProfilePresentation:
        presentation = _build_account_profile(self.bot, link)
        await self._edit_presentation(interaction, presentation, link)
        return presentation

    async def _edit_presentation(
        self,
        interaction: discord.Interaction,
        presentation: AccountProfilePresentation,
        link: VerificationLink,
    ) -> None:
        await interaction.edit_original_response(
            content=None,
            embed=presentation.embed,
            view=self._new_view(presentation, link),
        )

    async def _edit_stale_profile(self, interaction: discord.Interaction) -> None:
        await interaction.edit_original_response(
            content=_ACCOUNT_PROFILE_STALE_MESSAGE,
            embed=None,
            view=None,
        )

    async def _watch_rank_refresh(
        self,
        interaction: discord.Interaction,
        *,
        initial_state: AccountProfileState,
        baseline_rank_last_checked_at: datetime | None,
    ) -> None:
        guild_id = interaction.guild_id or 0
        current_state = initial_state
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ACCOUNT_PROFILE_REFRESH_WATCH_TIMEOUT_SECONDS

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                link = await self.bot.verifications.get_by_user(guild_id, self.owner_id)
                if link is None or not self._matches_current_link(link):
                    await self._edit_stale_profile(interaction)
                    return
                if not _same_timestamp(
                    link.rank_last_checked_at,
                    baseline_rank_last_checked_at,
                ):
                    await self._edit_profile(interaction, link)
                    return
                presentation = _build_account_profile(self.bot, link)
                if presentation.state in _ACCOUNT_PROFILE_PENDING_STATES:
                    set_account_profile_status(
                        presentation.embed,
                        _ACCOUNT_PROFILE_REFRESH_TIMEOUT_MESSAGE,
                    )
                await self._edit_presentation(interaction, presentation, link)
                return

            await asyncio.sleep(min(_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS, remaining))
            link = await self.bot.verifications.get_by_user(guild_id, self.owner_id)
            if link is None or not self._matches_current_link(link):
                await self._edit_stale_profile(interaction)
                return

            if not _same_timestamp(
                link.rank_last_checked_at,
                baseline_rank_last_checked_at,
            ):
                await self._edit_profile(interaction, link)
                return

            presentation = _build_account_profile(self.bot, link)
            if presentation.state not in _ACCOUNT_PROFILE_PENDING_STATES:
                await self._edit_presentation(interaction, presentation, link)
                return
            if presentation.state is not current_state:
                await self._edit_presentation(interaction, presentation, link)
                current_state = presentation.state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Nie możesz użyć przycisków w profilu innej osoby.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Zweryfikuj konto",
        style=discord.ButtonStyle.green,
        custom_id="account-profile:verify:v1",
    )
    async def verify_account(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[AccountProfileView],
    ) -> None:
        await self.start_verification(interaction)

    @discord.ui.button(
        label="Odśwież rangę",
        style=discord.ButtonStyle.blurple,
        custom_id="account-profile:rank-refresh:v1",
    )
    async def refresh_rank(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[AccountProfileView],
    ) -> None:
        if self._refresh_in_progress:
            await interaction.response.send_message(
                "Sprawdzamy możliwość odświeżenia. Poczekaj chwilę.",
                ephemeral=True,
            )
            return
        self._refresh_in_progress = True
        button.disabled = True
        button.label = REFRESH_RUNNING_BUTTON_LABEL
        button.style = discord.ButtonStyle.secondary
        request_recorded = False
        checking_cooldown = False
        try:
            await interaction.response.defer()
            if (
                self.expected_puuid is None
                or self.expected_platform is None
                or self.expected_created_at is None
            ):
                await self._edit_stale_profile(interaction)
                return
            result = await self.bot.verifications.request_rank_refresh(
                interaction.guild_id or 0,
                self.owner_id,
                cooldown_seconds=(self.bot.settings.rank_refresh_button_cooldown_seconds),
                source="user",
                expected_puuid=self.expected_puuid,
                expected_platform=self.expected_platform,
                expected_created_at=self.expected_created_at,
            )
            if result.status in {
                RankRefreshRequestStatus.LINK_CHANGED,
                RankRefreshRequestStatus.NOT_LINKED,
            }:
                await self._edit_stale_profile(interaction)
                return
            checking_cooldown = result.status is RankRefreshRequestStatus.COOLDOWN
            request_recorded = not checking_cooldown
            link = await self.bot.verifications.get_by_user(
                interaction.guild_id or 0,
                self.owner_id,
            )
            if link is None or not self._matches_current_link(link):
                await self._edit_stale_profile(interaction)
                return
            if not _same_timestamp(
                link.rank_last_checked_at,
                result.baseline_rank_last_checked_at,
            ):
                await self._edit_profile(interaction, link)
                return
            presentation = _build_account_profile(self.bot, link)
            if result.status is RankRefreshRequestStatus.COOLDOWN:
                if presentation.state in _ACCOUNT_PROFILE_PENDING_STATES:
                    request_recorded = True
                    await self._edit_presentation(interaction, presentation, link)
                    await self._watch_rank_refresh(
                        interaction,
                        initial_state=presentation.state,
                        baseline_rank_last_checked_at=result.baseline_rank_last_checked_at,
                    )
                    return
                if presentation.state in {
                    AccountProfileState.REFRESH_COOLDOWN,
                    AccountProfileState.SUCCESS,
                }:
                    self._restore_refresh_button(button)
                    await interaction.followup.send(
                        _rank_refresh_cooldown_message(result.retry_after_seconds),
                        ephemeral=True,
                    )
                    return
                await self._edit_presentation(interaction, presentation, link)
                return
            await self._edit_presentation(interaction, presentation, link)
            if (
                result.status in _ACCOUNT_PROFILE_OBSERVABLE_REQUESTS
                and presentation.state in _ACCOUNT_PROFILE_PENDING_STATES
            ):
                await self._watch_rank_refresh(
                    interaction,
                    initial_state=presentation.state,
                    baseline_rank_last_checked_at=result.baseline_rank_last_checked_at,
                )
        except Exception:
            logger.exception("Could not refresh account profile for %s", self.owner_id)
            if request_recorded:
                set_account_profile_status(
                    self.presentation.embed,
                    _ACCOUNT_PROFILE_REFRESH_OBSERVATION_ERROR_MESSAGE,
                )
                await interaction.edit_original_response(
                    content=None,
                    embed=self.presentation.embed,
                    view=self,
                )
                return
            self._restore_refresh_button(button)
            set_account_profile_status(
                self.presentation.embed,
                (
                    "Nie udało się sprawdzić możliwości odświeżenia.\nSpróbuj ponownie."
                    if checking_cooldown
                    else "Nie udało się rozpocząć odświeżania.\nSpróbuj ponownie."
                ),
            )
            await interaction.edit_original_response(
                content=None,
                embed=self.presentation.embed,
                view=self,
            )

    @discord.ui.button(
        label="Usuń powiązanie",
        style=discord.ButtonStyle.red,
        custom_id="account-profile:delete:v1",
    )
    async def remove_verification(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[AccountProfileView],
    ) -> None:
        await _show_delete_confirmation(
            self.bot,
            interaction,
            expected_puuid=self.expected_puuid,
            expected_platform=self.expected_platform,
            expected_created_at=self.expected_created_at,
        )


async def _show_account_profile(
    bot: MoonPoroBot,
    interaction: discord.Interaction,
    *,
    start_verification: VerificationStarter,
) -> None:
    if interaction.guild_id != bot.settings.guild_id or not isinstance(
        interaction.user, discord.Member
    ):
        await interaction.response.send_message(
            "Profil możesz otworzyć tylko na serwerze Moon Poro.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        link = await bot.verifications.get_by_user(interaction.guild_id, interaction.user.id)
        presentation = _build_account_profile(bot, link)
        view = AccountProfileView(
            bot,
            owner_id=interaction.user.id,
            presentation=presentation,
            link=link,
            start_verification=start_verification,
        )
        await interaction.edit_original_response(embed=presentation.embed, view=view)
    except Exception:
        logger.exception("Could not open account profile for %s", interaction.user.id)
        await interaction.edit_original_response(
            content="Nie udało się otworzyć profilu. Spróbuj ponownie."
        )


class DeleteVerificationConfirmationView(discord.ui.View):
    def __init__(
        self,
        bot: MoonPoroBot,
        *,
        owner_id: int,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
    ) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.owner_id = owner_id
        self.expected_puuid = expected_puuid
        self.expected_platform = expected_platform
        self.expected_created_at = expected_created_at

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Nie możesz potwierdzić usunięcia powiązania innej osoby.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Tak, usuń powiązanie",
        style=discord.ButtonStyle.red,
        custom_id="verification:delete:confirm:v1",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[DeleteVerificationConfirmationView],
    ) -> None:
        await interaction.response.edit_message(content="Usuwanie powiązania…", view=None)
        try:
            message = await _remove_user_verification(
                self.bot,
                interaction,
                expected_puuid=self.expected_puuid,
                expected_platform=self.expected_platform,
                expected_created_at=self.expected_created_at,
            )
        except Exception:
            logger.exception("Could not remove verification for %s", self.owner_id)
            message = (
                "Nie udało się usunąć powiązania. Otwórz `/profil`, aby sprawdzić aktualny stan."
            )
        await interaction.edit_original_response(content=message, view=None)

    @discord.ui.button(
        label="Anuluj",
        style=discord.ButtonStyle.secondary,
        custom_id="verification:delete:cancel:v1",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[DeleteVerificationConfirmationView],
    ) -> None:
        await interaction.response.edit_message(
            content="Powiązanie nie zostało usunięte.", view=None
        )


async def _show_delete_confirmation(
    bot: MoonPoroBot,
    interaction: discord.Interaction,
    *,
    expected_puuid: str | None = None,
    expected_platform: str | None = None,
    expected_created_at: datetime | None = None,
) -> None:
    if interaction.guild_id != bot.settings.guild_id or not isinstance(
        interaction.user, discord.Member
    ):
        await interaction.response.send_message(
            "Powiązanie możesz usunąć tylko na serwerze Moon Poro.", ephemeral=True
        )
        return
    link = await bot.verifications.get_by_user(interaction.guild_id, interaction.user.id)
    if link is None:
        await interaction.response.send_message("Nie masz połączonego konta Riot.", ephemeral=True)
        return
    if (
        (expected_puuid is not None and link.puuid != expected_puuid)
        or (expected_platform is not None and link.platform != expected_platform)
        or (expected_created_at is not None and link.created_at != expected_created_at)
    ):
        await interaction.response.send_message(
            _ACCOUNT_PROFILE_STALE_MESSAGE,
            ephemeral=True,
        )
        return
    if link.puuid is None:
        await interaction.response.send_message("Nie masz połączonego konta Riot.", ephemeral=True)
        return
    await interaction.response.send_message(
        (
            "Czy na pewno chcesz usunąć powiązanie z kontem Riot?\n\n"
            "Rola „Zweryfikowany” zostanie usunięta. "
            "Role regionu, rangi i użytkownika pozostaną bez zmian."
        ),
        view=DeleteVerificationConfirmationView(
            bot,
            owner_id=interaction.user.id,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        ),
        ephemeral=True,
    )


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
        label="Zweryfikuj konto",
        style=discord.ButtonStyle.green,
        custom_id="verification:start:rso:v1",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[VerificationStartView],
    ) -> None:
        await self.begin_verification(interaction)

    async def begin_verification(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.bot.settings.guild_id:
            await interaction.response.send_message(
                "Weryfikację możesz rozpocząć tylko na serwerze Moon Poro.", ephemeral=True
            )
            return
        retry_after = self.cooldowns.update_rate_limit(interaction)
        if retry_after:
            await interaction.response.send_message(
                f"Spróbuj ponownie za {math.ceil(retry_after)} s.", ephemeral=True
            )
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Weryfikację rozpocznij na serwerze Discord.", ephemeral=True
            )
            return
        existing_link = await self.bot.verifications.get_by_user(
            interaction.guild.id, interaction.user.id
        )
        if (
            existing_link is not None
            and getattr(existing_link, "deletion_requested_at", None) is not None
        ):
            await interaction.response.send_message(
                "Usuwanie poprzedniego powiązania jeszcze trwa. Spróbuj ponownie za chwilę.",
                ephemeral=True,
            )
            return
        if (
            member_has_role(
                interaction.user, self.bot.settings.verified_role_name, self.bot.settings
            )
            or existing_link is not None
        ):
            await interaction.response.send_message(
                "Masz już połączone konto Riot. Aby połączyć inne, najpierw usuń "
                "obecne powiązanie w `/profil`.",
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
                label="Zaloguj się przez Riot",
                style=discord.ButtonStyle.link,
                url=verification_url,
            )
        )
        minutes = self.bot.settings.rso_session_ttl_seconds // 60
        embed = discord.Embed(
            title="Połącz konto Riot",
            description=(
                "Kliknij przycisk poniżej i zaloguj się na stronie Riot.\n"
                "Moon Poro nie zobaczy Twojego hasła. "
                f"Jednorazowy link wygaśnie za {minutes} min."
            ),
            colour=discord.Colour.from_rgb(116, 211, 224),
        )
        embed.set_footer(text="Po zalogowaniu wróć do Discorda. Role zostaną nadane automatycznie.")
        await interaction.response.send_message(embed=embed, view=link_view, ephemeral=True)

    @discord.ui.button(
        label="Moje konto",
        style=discord.ButtonStyle.secondary,
        custom_id="verification:account-profile:v1",
    )
    async def account_profile(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[VerificationStartView],
    ) -> None:
        await _show_account_profile(
            self.bot,
            interaction,
            start_verification=self.begin_verification,
        )

    @discord.ui.button(
        label="Odśwież rangę",
        style=discord.ButtonStyle.blurple,
        custom_id="verification:rank-refresh:v1",
    )
    async def refresh_rank(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[VerificationStartView],
    ) -> None:
        await _request_rank_refresh_from_panel(self.bot, interaction)

    @discord.ui.button(
        label="Usuń powiązanie",
        style=discord.ButtonStyle.red,
        custom_id="verification:delete:v1",
    )
    async def remove_verification(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[VerificationStartView],
    ) -> None:
        await _show_delete_confirmation(self.bot, interaction)


class VerificationCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self._managed_role_updates: set[int] = set()
        self.rso_start_view = VerificationStartView(bot)
        bot.add_view(self.rso_start_view)
        self.refresh_verified.change_interval(
            seconds=bot.settings.rank_refresh_worker_interval_seconds
        )
        self.complete_rso_verifications.change_interval(
            seconds=bot.settings.rso_completion_interval_seconds
        )
        self.report_riot_monitoring.change_interval(
            seconds=bot.settings.riot_monitoring_interval_seconds
        )
        self.refresh_verified.start()
        self.verification_maintenance.start()
        self.complete_rso_verifications.start()
        self.report_riot_monitoring.start()

    async def cog_unload(self) -> None:
        self.refresh_verified.cancel()
        self.verification_maintenance.cancel()
        self.complete_rso_verifications.cancel()
        self.report_riot_monitoring.cancel()

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

        link = await self.bot.verifications.get_by_user(record.guild_id, record.discord_user_id)
        if (
            link is None
            or link.deletion_requested_at is not None
            or link.puuid != record.puuid
            or link.platform != record.platform
        ):
            await self.bot.verification_sessions.fail_discord(
                record.id, "VERIFICATION_LINK_CHANGED"
            )
            return
        cached = _cached_rank_leagues(link) if link is not None else None
        cached_checked_at = link.rank_last_checked_at if link is not None else None
        try:
            if (
                cached_checked_at is not None
                and cached_checked_at >= record.created_at
                and cached is not None
            ):
                role_leagues = cached
                checked_at = cached_checked_at
            else:
                leagues = await _get_leagues(self.bot, record.platform, record.puuid)
                recorded = await _record_leagues(self.bot, link, leagues)
                if recorded is None:
                    await self._retry_rso_completion(record, "VERIFICATION_LINK_MISSING")
                    return
                role_leagues, checked_at = recorded
            await self.apply_verified_roles(member, record.platform, role_leagues)
        except RiotAPIUnavailable:
            await self._retry_rso_completion(record, "RIOT_API_UNAVAILABLE")
            return
        except discord.HTTPException:
            await self.bot.verifications.retry_rank_role_sync(
                record.guild_id,
                record.discord_user_id,
                base_delay_seconds=self.bot.settings.rank_refresh_retry_base_seconds,
                expected_rank_last_checked_at=checked_at,
                expected_puuid=link.puuid,
                expected_platform=link.platform,
                expected_created_at=link.created_at,
            )
            await self._retry_rso_completion(record, "DISCORD_ROLES_UNAVAILABLE")
            return
        if not await _reconcile_applied_roles(self, member, link):
            await self.bot.verification_sessions.fail_discord(
                record.id, "VERIFICATION_LINK_CHANGED"
            )
            return
        await self.bot.verifications.acknowledge_rank_role_sync(
            record.guild_id,
            record.discord_user_id,
            expected_rank_last_checked_at=checked_at,
            expected_puuid=link.puuid,
            expected_platform=link.platform,
            expected_created_at=link.created_at,
        )

        audit_message_id: int | None = None
        channel_id = self.bot.settings.zweryfikowani_channel_id
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.abc.Messageable):
            embed = discord.Embed(title="Zweryfikowane konto Riot", colour=discord.Colour.green())
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
            record.id,
            message_id=audit_message_id,
            channel_id=channel_id if audit_message_id is not None else None,
        )
        if not completed:
            current = await self.bot.verifications.get_by_user(
                record.guild_id,
                record.discord_user_id,
            )
            await _remove_verified_marker_durably(
                self,
                member,
                reason="Weryfikacja RSO została anulowana w trakcie finalizacji",
                observed_current=current,
            )
            return
        with suppress(discord.Forbidden, discord.HTTPException):
            await member.send(
                "Twoje konto Riot zostało zweryfikowane. "
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
            await _retry_next_verified_marker_cleanup(self)
            await _retry_next_rso_audit_cleanup(self)
            await _retry_next_verification_deletion(self)
            await _retry_next_rank_role_sync(self)
            await _refresh_next_verified(self)
        except Exception:
            logger.exception("Unexpected rank refresh worker error; next run will retry")

    @tasks.loop(minutes=5, reconnect=True)
    async def report_riot_monitoring(self) -> None:
        await _report_riot_monitoring(self.bot)

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

    @report_riot_monitoring.before_loop
    async def before_riot_monitoring(self) -> None:
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
        embed = discord.Embed(
            title="Weryfikacja konta League of Legends",
            description=(
                "Połącz konto przez oficjalne logowanie Riot. Bot zapisze Riot ID i region "
                "oraz będzie aktualizował rangę Solo/Duo i role."
            ),
            colour=discord.Colour.from_rgb(116, 211, 224),
        )
        embed.add_field(
            name="Prywatność",
            value=f"[Polityka prywatności]({privacy_url})",
            inline=False,
        )
        embed.set_footer(text="Logowanie odbywa się na stronie Riot.")
        await interaction.response.send_message(embed=embed, view=self.rso_start_view)

    @app_commands.command(
        name="usun_weryfikacje", description="Usuwa powiązanie z Twoim kontem Riot"
    )
    async def remove_own_verification(self, interaction: discord.Interaction) -> None:
        await _show_delete_confirmation(self.bot, interaction)

    @app_commands.command(name="profil", description="Pokazuje Twoje konto Riot")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction) -> None:
        await _show_account_profile(
            self.bot,
            interaction,
            start_verification=self.rso_start_view.begin_verification,
        )

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
            await interaction.followup.send("Riot jest chwilowo niedostępny.", ephemeral=True)
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
            await interaction.followup.send("Riot jest chwilowo niedostępny.", ephemeral=True)
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
            await interaction.followup.send("Riot jest chwilowo niedostępny.", ephemeral=True)
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
