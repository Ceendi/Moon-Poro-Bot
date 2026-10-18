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
)
from moon_poro.bot import MoonPoroBot
from moon_poro.member_role_lock import member_role_lock
from moon_poro.models import VerificationLink, VerificationSession
from moon_poro.permissions import administrator_only, is_administrator
from moon_poro.rank_refresh import (
    RankSnapshot,
    decide_rank_refresh,
    effective_refresh_interval,
    solo_rank_snapshot,
)
from moon_poro.repositories import (
    RankRefreshRequestStatus,
    VerificationDeletionAccessLog,
    VerificationDeletionPolicy,
    VerificationDeletionProcessStatus,
    VerificationDeletionRequestStatus,
    VerificationLinkIdentity,
    VerificationSnapshotChanged,
)
from moon_poro.riot import (
    API_SERVERS,
    RANK_TO_ROLE,
    RIOT_LEAGUE_AUTH_SCOPE,
    SERVER_TRANSLATION,
    RiotAPIInvalidResponse,
    RiotAPINotFound,
    RiotAPIUnavailable,
    get_discord_rank_role,
    get_rank_from_leagues,
    riot_api_call,
)
from moon_poro.roles import find_role, member_has_role, member_roles_named
from moon_poro.time_utils import timestamps_match
from moon_poro.verification_sessions import SessionAlreadyUsed

logger = logging.getLogger("moon_poro.verification")

# Members whose rank refresh is already followed by a private reply.
_ACTIVE_RANK_REFRESH_WATCHES: set[tuple[int, int]] = set()

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
_ACCOUNT_PROFILE_STALE_MESSAGE = (
    "Ta wiadomość jest już nieaktualna. Otwórz „Moje konto” jeszcze raz."
)
_ACCOUNT_PROFILE_REFRESH_TIMEOUT_MESSAGE = (
    "Odświeżanie trwa dłużej niż zwykle. Otwórz „Moje konto” jeszcze raz za chwilę."
)
_ACCOUNT_PROFILE_REFRESH_OBSERVATION_ERROR_MESSAGE = (
    "Nie udało się pokazać wyniku. Odświeżanie może jeszcze trwać, sprawdź „Moje konto” za chwilę."
)
_PANEL_LINK_CHANGED_MESSAGE = (
    "Powiązanie konta zmieniło się. Sprawdź aktualne dane w sekcji „Moje konto”."
)
_PANEL_NOT_LINKED_MESSAGE = "Najpierw kliknij „Zweryfikuj konto” i połącz konto Riot."
_ORPHAN_VERIFIED_ROLE_TIMEOUT_SECONDS = 10.0
_DEFAULT_PRIVACY_POLICY_URL = "https://moonporo.pl/privacy/"
_DEFAULT_TERMS_OF_SERVICE_URL = "https://moonporo.pl/terms/"


def _rank_refresh_cooldown_message(retry_after_seconds: int | None) -> str:
    if retry_after_seconds is None or retry_after_seconds <= 0:
        return "⏳ **Rangę będzie można odświeżyć za chwilę.**"
    seconds = retry_after_seconds
    if seconds >= 60:
        minutes = math.ceil(seconds / 60)
        return f"⏳ **Rangę możesz odświeżyć ponownie za około {minutes} min.**"
    return f"⏳ **Rangę możesz odświeżyć ponownie za {seconds} s.**"


def _verification_legal_links(bot: MoonPoroBot) -> str:
    privacy_url = str(bot.settings.privacy_policy_url or _DEFAULT_PRIVACY_POLICY_URL)
    terms_url = str(bot.settings.terms_of_service_url or _DEFAULT_TERMS_OF_SERVICE_URL)
    return f"[Polityka prywatności]({privacy_url}) · [Warunki korzystania]({terms_url})"


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


async def _sync_riot_id(
    bot: MoonPoroBot,
    link: VerificationLink,
    *,
    expected_claimed_at: datetime,
) -> bool:
    puuid = link.puuid
    if not puuid:
        return False
    try:
        account = await riot_api_call(
            lambda: bot.riot_client.get_account_v1_by_puuid(
                puuid=puuid,
                region=API_SERVERS[link.platform],
            ),
            not_found=None,
        )
    except RiotAPIUnavailable:
        return False
    if not isinstance(account, dict):
        return False
    game_name = account.get("gameName")
    tag_line = account.get("tagLine")
    if (
        not isinstance(game_name, str)
        or not game_name.strip()
        or not isinstance(tag_line, str)
        or not tag_line.strip()
    ):
        return False
    stored = await bot.verifications.sync_riot_id_if_current(
        link.guild_id,
        link.discord_user_id,
        game_name=game_name,
        tag_line=tag_line,
        expected_puuid=puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
        expected_claimed_at=expected_claimed_at,
    )
    if stored:
        link.riot_game_name = game_name.strip()[:100]
        link.riot_tag_line = tag_line.strip()[:20]
    return stored


_reported_missing_roles: set[tuple[int | None, str]] = set()


def _warn_missing_role(guild: discord.Guild, role_name: str) -> None:
    """Log a configured role that cannot be found, once per guild and process."""

    key = (getattr(guild, "id", None), role_name)
    if key in _reported_missing_roles:
        return
    _reported_missing_roles.add(key)
    logger.warning(
        "Verification role %r was not found in guild %s; check ROLE_IDS and role names",
        role_name,
        key[0],
    )


def _find_verification_role(
    guild: discord.Guild, role_name: str, bot: MoonPoroBot
) -> discord.Role | None:
    role = find_role(guild, role_name, bot.settings)
    if role is None:
        _warn_missing_role(guild, role_name)
    return role


async def _apply_verified_roles(
    bot: MoonPoroBot,
    member: discord.Member,
    platform: str,
    leagues: LeagueEntries | None,
) -> None:
    # Gateway and interaction members are snapshots. The caller serializes role
    # writes before taking the link row lock; fetch only after those guards.
    member = await member.guild.fetch_member(member.id)
    settings = bot.settings
    # A missing cache is unknown, not a Riot-confirmed unranked account.
    rank_role = None
    keep_current_rank = False
    if leagues is not None:
        try:
            rank = get_rank_from_leagues(leagues)
        except RiotAPIInvalidResponse:
            # Fresh Riot data is validated by the client, so only a corrupt cached
            # tier lands here. Treat it as unknown rather than failing the restore.
            logger.warning("Ignoring an unknown cached rank for Discord user %s", member.id)
            keep_current_rank = True
        else:
            rank_role = get_discord_rank_role(member.guild, rank, settings)
            if rank_role is None:
                _warn_missing_role(member.guild, RANK_TO_ROLE.get(rank.upper(), "Unranked"))
    server_name = SERVER_TRANSLATION[platform]
    server_role = _find_verification_role(member.guild, server_name, bot)
    verified_role = _find_verification_role(member.guild, settings.verified_role_name, bot)
    member_role = _find_verification_role(member.guild, settings.member_role_name, bot)

    managed_names = set(settings.lol_ranks + settings.lol_servers)
    no_lol_role_name = getattr(settings, "no_lol_role_name", None)
    if no_lol_role_name:
        managed_names.add(no_lol_role_name)
    desired = {role for role in (rank_role, server_role) if role is not None}
    if keep_current_rank:
        desired.update(member_roles_named(member, settings.lol_ranks, settings))
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


async def _run_active_link_role_update(
    bot: MoonPoroBot,
    link: VerificationLink,
    operation: Callable[[], Awaitable[None]],
    *,
    expected_rank_last_checked_at: datetime | None = None,
) -> bool:
    async with member_role_lock(link.guild_id, link.discord_user_id):
        return await bot.verifications.run_verification_role_update_with_identity(
            link.guild_id,
            link.discord_user_id,
            identity=VerificationLinkIdentity.from_link(link),
            operation=operation,
            expected_rank_last_checked_at=(
                expected_rank_last_checked_at
                if expected_rank_last_checked_at is not None
                else getattr(link, "rank_last_checked_at", None)
            ),
        )


async def _run_pending_deletion_role_cleanup(
    bot: MoonPoroBot,
    link: VerificationLink,
    operation: Callable[[], Awaitable[None]],
) -> bool:
    requested_at = link.deletion_requested_at
    if requested_at is None:
        return False
    async with member_role_lock(link.guild_id, link.discord_user_id):
        return await bot.verifications.run_verification_deletion_role_cleanup_with_identity(
            link.guild_id,
            link.discord_user_id,
            identity=VerificationLinkIdentity.from_link(link),
            expected_requested_at=requested_at,
            expected_remove_rank_region_roles=link.deletion_remove_rank_region_roles,
            operation=operation,
        )


async def _schedule_role_sync_retry(bot: MoonPoroBot, link: VerificationLink) -> int | None:
    """Hand a failed role write for the observed snapshot to the durable worker."""

    return await bot.verifications.retry_rank_role_sync(
        link.guild_id,
        link.discord_user_id,
        base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
        expected_rank_last_checked_at=getattr(link, "rank_last_checked_at", None),
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )


async def _clear_orphan_verified_role(bot: MoonPoroBot, member: discord.Member) -> bool:
    """Remove a Verified role left without any verification link.

    Return ``False`` when a link exists after waiting for the member's role lock,
    so the caller must not start another verification.
    """

    guild_id = member.guild.id
    try:
        async with asyncio.timeout(_ORPHAN_VERIFIED_ROLE_TIMEOUT_SECONDS):
            async with member_role_lock(guild_id, member.id):
                # A writer holding this lock may have just finished a verification.
                if await bot.verifications.get_by_user(guild_id, member.id) is not None:
                    return False
                await _remove_verified_marker(
                    bot,
                    member,
                    reason="Usunięcie roli weryfikacji bez połączonego konta Riot",
                )
    except (discord.HTTPException, TimeoutError):
        # The new verification sets the role again; a failure here is cosmetic.
        logger.exception("Could not remove an orphaned Verified role from %s", member.id)
    return True


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


async def _remove_roles_for_pending_deletion(
    bot: MoonPoroBot,
    member: discord.Member,
    link: VerificationLink,
    *,
    reason: str,
    refresh_member: bool = True,
) -> None:
    if refresh_member:
        member = await member.guild.fetch_member(member.id)
    if link.deletion_remove_rank_region_roles:
        await _remove_verified_roles(bot, member, reason=reason)
    else:
        await _remove_verified_marker(bot, member, reason=reason)


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
    *,
    expected_claimed_at: datetime | None,
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
        expected_claimed_at=expected_claimed_at,
        expected_rank_last_checked_at=link.rank_last_checked_at,
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
    identity = VerificationLinkIdentity.from_link(link)
    for _attempt in range(4):
        try:
            return await _ensure_active_link_roles_once(cog, member, link)
        except VerificationSnapshotChanged:
            current = await cog.bot.verifications.get_by_user(member.guild.id, member.id)
            if (
                current is None
                or current.deletion_requested_at is not None
                or current.puuid != identity.puuid
                or current.platform != identity.platform
                or not timestamps_match(current.created_at, identity.created_at)
            ):
                return False
            link = current
    # Every newer snapshot has its own durable role-sync intent. Leave that
    # intent untouched if an unusually busy account exhausts this local retry.
    return False


async def _ensure_active_link_roles_once(
    cog: VerificationCog,
    member: discord.Member,
    link: VerificationLink,
) -> bool:
    cached = _cached_rank_leagues(link)
    if cached is None:
        try:
            applied = await _run_active_link_role_update(
                cog.bot,
                link,
                lambda: _apply_verified_roles(cog.bot, member, link.platform, None),
            )
        except (discord.HTTPException, TimeoutError):
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
            return False
        if not applied:
            return False
        await cog.bot.verifications.schedule_rank_refresh_now(member.guild.id, member.id)
    else:
        try:
            applied = await _run_active_link_role_update(
                cog.bot,
                link,
                lambda: cog.apply_verified_roles(member, link.platform, cached),
            )
        except (discord.HTTPException, TimeoutError):
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
            if not applied:
                return False
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
            async with member_role_lock(guild_id, member.id):
                try:
                    fresh_member = await member.guild.fetch_member(member.id)
                except discord.NotFound:
                    fresh_member = None
                # The observed link and Member can predate the writer that held
                # this lock. Recheck after waiting and fetching; never strip the
                # marker of an active replacement. Restore it outside this lock
                # because the guarded role writer acquires the same lock.
                current = await bot.verifications.get_by_user(guild_id, member.id)
                if current is not None and current.deletion_requested_at is None:
                    continue
                if fresh_member is not None:
                    await _remove_verified_marker(bot, fresh_member, reason=reason)
                current = await bot.verifications.get_by_user(guild_id, member.id)
                if current is None or current.deletion_requested_at is not None:
                    await bot.verifications.acknowledge_verified_marker_cleanup(
                        guild_id,
                        member.id,
                        expected_generation=expected_generation,
                    )
                    return
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
    # Without a cached rank, restore the verified, member and region roles only.
    # The rank refresh queue adds the rank role once Riot confirms it.
    leagues = _cached_rank_leagues(link)
    try:
        applied = await _run_active_link_role_update(
            bot,
            link,
            lambda: cog.apply_verified_roles(member, link.platform, leagues),
        )
    except VerificationSnapshotChanged:
        # The newer snapshot owns its own synchronization intent and ACK.
        return
    except (discord.HTTPException, TimeoutError):
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
    if not applied:
        return
    if not await _reconcile_applied_roles(cog, member, link):
        return
    acknowledged = await bot.verifications.acknowledge_rank_role_sync(
        guild.id,
        link.discord_user_id,
        expected_rank_last_checked_at=link.rank_last_checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )
    if leagues is None and acknowledged:
        await bot.verifications.schedule_rank_refresh_now(guild.id, link.discord_user_id)


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
    channel: discord.abc.GuildChannel | discord.abc.PrivateChannel | discord.Thread | None = (
        guild.get_channel(record.channel_id) if guild is not None else None
    )
    try:
        async with asyncio.timeout(10):
            if channel is None:
                channel = await bot.fetch_channel(record.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise RuntimeError("Verification audit cleanup channel is unavailable")
            await channel.get_partial_message(record.message_id).delete()
    except discord.NotFound:
        pass
    except (discord.HTTPException, RuntimeError, TimeoutError):
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
    if not bot.riot_auth_breaker.can_attempt(scope=RIOT_LEAGUE_AUTH_SCOPE):
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
        logger.error(
            "Claimed rank refresh has no PUUID for Discord user %s",
            link.discord_user_id,
        )
        return
    claimed_at = link.rank_refresh_claimed_at
    if claimed_at is None:
        logger.error(
            "Claimed rank refresh has no ownership token for Discord user %s",
            link.discord_user_id,
        )
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
            expected_claimed_at=claimed_at,
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
            expected_claimed_at=claimed_at,
        )
        logger.warning(
            "Riot League-v4 returned 404 for Discord user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return
    except RiotAPIUnavailable as error:
        # A single 403 does not open the league gate, so it backs off like any
        # other failure instead of putting the same link straight back in the queue.
        if error.status == 401 or not bot.riot_auth_breaker.can_attempt(
            scope=RIOT_LEAGUE_AUTH_SCOPE
        ):
            await bot.verifications.release_rank_refresh_claim(
                guild.id,
                link.discord_user_id,
                expected_puuid=link.puuid,
                expected_platform=link.platform,
                expected_created_at=link.created_at,
                expected_claimed_at=claimed_at,
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
            expected_claimed_at=claimed_at,
        )
        logger.warning(
            "Could not refresh Riot rank for Discord user %s; retrying in %s seconds",
            member.id,
            delay,
        )
        return

    await _sync_riot_id(
        bot,
        link,
        expected_claimed_at=claimed_at,
    )
    recorded = await _record_leagues(
        bot,
        link,
        leagues,
        expected_claimed_at=claimed_at,
    )
    if recorded is None:
        return
    cached_leagues, checked_at = recorded
    try:
        applied = await _run_active_link_role_update(
            bot,
            link,
            lambda: cog.apply_verified_roles(member, link.platform, cached_leagues),
            expected_rank_last_checked_at=checked_at,
        )
    except VerificationSnapshotChanged:
        return
    except (discord.HTTPException, TimeoutError):
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
    if not applied:
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
    auth = bot.riot_auth_breaker.snapshot(scope=RIOT_LEAGUE_AUTH_SCOPE)
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
    if link is None:
        return
    if getattr(link, "deletion_requested_at", None) is not None:
        try:
            await _run_pending_deletion_role_cleanup(
                cog.bot,
                link,
                lambda: _remove_roles_for_pending_deletion(
                    cog.bot,
                    member,
                    link,
                    reason="Dokończenie usuwania weryfikacji Riot",
                ),
            )
        except (discord.HTTPException, TimeoutError):
            # The deletion queue retries this cleanup on its own schedule.
            logger.exception("Could not enforce pending verification deletion for %s", member.id)
        return
    if not link.puuid:
        return
    # Without a cached rank, restore Verified, member and region roles only and
    # let the rank refresh queue add the rank role once Riot confirms it.
    leagues = _cached_rank_leagues(link)
    try:
        applied = await _run_active_link_role_update(
            cog.bot,
            link,
            lambda: cog.apply_verified_roles(member, link.platform, leagues),
        )
    except VerificationSnapshotChanged:
        await _ensure_active_link_roles(cog, member, link)
        return
    except (discord.HTTPException, TimeoutError):
        delay = await _schedule_role_sync_retry(cog.bot, link)
        logger.exception(
            "Could not restore verification roles for %s; retrying in %s seconds",
            member.id,
            delay,
        )
    else:
        if not applied:
            return
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
    if leagues is None:
        await cog.bot.verifications.request_rank_refresh(
            member.guild.id,
            member.id,
            cooldown_seconds=cog.bot.settings.rank_refresh_manual_priority_cooldown_seconds,
            source="role_tamper",
        )
        return
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
    if no_lol_role_name := getattr(settings, "no_lol_role_name", None):
        protected_names.add(no_lol_role_name)
    before_roles = set(member_roles_named(before, protected_names, settings))
    after_roles = set(member_roles_named(after, protected_names, settings))
    if before_roles == after_roles:
        return

    link = await cog.bot.verifications.get_by_user(after.guild.id, after.id)
    if link is None:
        return
    if getattr(link, "deletion_requested_at", None) is not None:
        try:
            await _run_pending_deletion_role_cleanup(
                cog.bot,
                link,
                lambda: _remove_roles_for_pending_deletion(
                    cog.bot,
                    after,
                    link,
                    reason="Dokończenie usuwania weryfikacji Riot",
                ),
            )
        except (discord.HTTPException, TimeoutError):
            # The deletion queue retries this cleanup on its own schedule.
            logger.exception("Could not enforce pending verification deletion for %s", after.id)
        return
    if not link.puuid:
        return
    leagues = _cached_rank_leagues(link)
    if leagues is not None:
        try:
            applied = await _run_active_link_role_update(
                cog.bot,
                link,
                lambda: cog.apply_verified_roles(after, link.platform, leagues),
            )
        except VerificationSnapshotChanged:
            await _ensure_active_link_roles(cog, after, link)
            return
        except (discord.HTTPException, TimeoutError):
            delay = await _schedule_role_sync_retry(cog.bot, link)
            logger.exception(
                "Could not restore cached verification roles for %s; retrying in %s seconds",
                after.id,
                delay,
            )
        else:
            if not applied:
                return
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

    try:
        applied = await _run_active_link_role_update(
            cog.bot,
            link,
            lambda: cog.apply_verified_roles(after, link.platform, None),
        )
    except VerificationSnapshotChanged:
        await _ensure_active_link_roles(cog, after, link)
        return
    except (discord.HTTPException, TimeoutError):
        delay = await _schedule_role_sync_retry(cog.bot, link)
        logger.exception(
            "Could not restore canonical verification roles for %s; retrying in %s seconds",
            after.id,
            delay,
        )
        return
    if not applied:
        return
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
    identity: VerificationLinkIdentity,
) -> str:
    guild_id = interaction.guild_id or 0
    result = await bot.verifications.request_verification_deletion_with_identity(
        guild_id,
        interaction.user.id,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    if result.status in {
        VerificationDeletionRequestStatus.LINK_CHANGED,
        VerificationDeletionRequestStatus.NOT_LINKED,
    }:
        return _ACCOUNT_PROFILE_STALE_MESSAGE
    if result.status in {
        VerificationDeletionRequestStatus.ALREADY_REQUESTED,
        VerificationDeletionRequestStatus.POLICY_CONFLICT,
    }:
        return "Usuwanie tego powiązania już trwa."
    link = result.link
    if result.status is not VerificationDeletionRequestStatus.REQUESTED or link is None:
        return "Nie udało się rozpocząć usuwania. Spróbuj ponownie."
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    completed = await _process_requested_verification_deletion(
        bot,
        guild=interaction.guild,
        member=member,
        link=link,
    )
    if not completed:
        return "Usuwanie powiązania jeszcze trwa. Bot dokończy je sam."
    if identity.puuid is None:
        return "Usunięto poprzednie powiązanie. Możesz teraz ponownie połączyć konto Riot."
    return (
        "Usunięto powiązanie z kontem Riot oraz rolę „Zweryfikowany”. "
        "Role regionu i rangi oraz rola „Użytkownik” zostają."
    )


async def _process_requested_verification_deletion(
    bot: MoonPoroBot,
    *,
    guild: discord.Guild | None,
    member: discord.Member | None,
    link: VerificationLink,
) -> bool:
    claimed_at = link.deletion_claimed_at
    deletion_requested_at = link.deletion_requested_at
    if claimed_at is None or deletion_requested_at is None:
        return False
    identity = VerificationLinkIdentity.from_link(link)
    await bot.verification_sessions.cancel_for_user(
        link.guild_id,
        link.discord_user_id,
        created_at_or_before=deletion_requested_at,
    )

    async def cleanup_discord() -> bool:
        if guild is None:
            logger.warning("Verification deletion cannot resolve its guild")
            return False
        try:
            resolved_member = await guild.fetch_member(link.discord_user_id)
        except discord.NotFound:
            resolved_member = None
        except discord.HTTPException:
            return False
        if resolved_member is not None:
            try:
                await _remove_roles_for_pending_deletion(
                    bot,
                    resolved_member,
                    link,
                    refresh_member=False,
                    reason=(
                        "Administracyjne usunięcie powiązania Riot"
                        if link.deletion_remove_rank_region_roles
                        else "Usunięcie weryfikacji przez użytkownika"
                    ),
                )
            except discord.HTTPException:
                return False

        if link.message_id:
            channel_id = link.audit_channel_id
            if not channel_id:
                logger.warning("Verification deletion is missing its audit channel id")
                return False
            channel: discord.abc.GuildChannel | discord.Thread | None = guild.get_channel(
                channel_id
            )
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.NotFound:
                    channel = None
                except discord.HTTPException:
                    return False
            if channel is None:
                return True
            if not isinstance(channel, discord.abc.Messageable):
                logger.warning("Verification deletion audit target is not messageable")
                return False
            try:
                await channel.get_partial_message(link.message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                return False
        return True

    async with member_role_lock(link.guild_id, link.discord_user_id):
        result = await bot.verifications.process_verification_deletion_with_identity(
            link.guild_id,
            link.discord_user_id,
            identity=identity,
            expected_claimed_at=claimed_at,
            base_delay_seconds=bot.settings.rank_refresh_retry_base_seconds,
            operation=cleanup_discord,
        )
    if result.status in {
        VerificationDeletionProcessStatus.DELETED,
        VerificationDeletionProcessStatus.ALREADY_DELETED,
    }:
        return True
    if result.status is VerificationDeletionProcessStatus.RETRY_SCHEDULED:
        logger.warning(
            "Verification deletion retry scheduled in %s seconds",
            result.retry_after_seconds,
        )
    else:
        logger.info("Verification deletion stopped before side effects: %s", result.status)
    return False


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
    await _process_requested_verification_deletion(
        bot,
        guild=guild,
        member=member,
        link=link,
    )


def _panel_refresh_blocker(link: VerificationLink | None) -> str | None:
    if link is None:
        return _PANEL_NOT_LINKED_MESSAGE
    if link.deletion_requested_at is not None:
        return "Usuwanie powiązania jeszcze trwa."
    if not link.puuid:
        return "Najpierw usuń poprzednie powiązanie w sekcji „Moje konto”."
    return None


async def _request_rank_refresh_from_panel(
    bot: MoonPoroBot,
    interaction: discord.Interaction,
    *,
    start_verification: VerificationStarter,
) -> None:
    """Run the public panel refresh through the private account profile view."""

    if interaction.guild_id != bot.settings.guild_id or not isinstance(
        interaction.user, discord.Member
    ):
        await interaction.response.send_message(
            "Rangę możesz odświeżyć tylko na serwerze Moon Poro.", ephemeral=True
        )
        return
    # The queue lookup and request use the database; answer Discord first.
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        link = await bot.verifications.get_by_user(interaction.guild_id, interaction.user.id)
    except Exception:
        logger.exception("Could not open rank refresh for %s", interaction.user.id)
        await interaction.edit_original_response(
            content="Nie udało się rozpocząć odświeżania. Spróbuj ponownie."
        )
        return
    blocker = _panel_refresh_blocker(link)
    if blocker is not None or link is None:
        await interaction.edit_original_response(content=blocker or _PANEL_NOT_LINKED_MESSAGE)
        return
    view = AccountProfileView(
        bot,
        owner_id=interaction.user.id,
        presentation=_build_account_profile(bot, link),
        link=link,
        start_verification=start_verification,
    )
    view.stale_message = _PANEL_LINK_CHANGED_MESSAGE
    await view.run_rank_refresh(interaction, deferred=True)


def _riot_id_from_account(account: RiotPayload) -> str | None:
    game_name = account.get("gameName")
    tag_line = account.get("tagLine")
    if not isinstance(game_name, str) or not isinstance(tag_line, str):
        return None
    if not game_name.strip() or not tag_line.strip():
        return None
    return f"{game_name.strip()}#{tag_line.strip()}"


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
        riot_authorization_unavailable=bot.riot_auth_breaker.snapshot(
            scope=RIOT_LEAGUE_AUTH_SCOPE
        ).blocked,
    )


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
        self.link_identity = VerificationLinkIdentity.from_link(link) if link is not None else None
        self.stale_message = _ACCOUNT_PROFILE_STALE_MESSAGE
        self._refresh_in_progress = False

        if presentation.state is AccountProfileState.UNVERIFIED:
            self.remove_item(self.refresh_rank)
            self.remove_item(self.remove_verification)
        elif presentation.state is AccountProfileState.INCOMPLETE_LEGACY:
            self.remove_item(self.verify_account)
            self.remove_item(self.refresh_rank)
            self.remove_verification.label = "Usuń poprzednie powiązanie"
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
        identity = self.link_identity
        return bool(
            link is not None
            and link.deletion_requested_at is None
            and identity is not None
            and link.puuid == identity.puuid
            and link.platform == identity.platform
            and timestamps_match(link.created_at, identity.created_at)
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
        *,
        content: str | None = None,
    ) -> None:
        await interaction.edit_original_response(
            content=content,
            embed=presentation.embed,
            view=self._new_view(presentation, link),
        )

    async def _edit_stale_profile(self, interaction: discord.Interaction) -> None:
        await interaction.edit_original_response(
            content=self.stale_message,
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
        watch_key = (guild_id, self.owner_id)
        if watch_key in _ACTIVE_RANK_REFRESH_WATCHES:
            # Another reply already follows this refresh; repeated clicks only
            # show the current state instead of polling the database again.
            return
        _ACTIVE_RANK_REFRESH_WATCHES.add(watch_key)
        try:
            await self._poll_rank_refresh(
                interaction,
                guild_id=guild_id,
                initial_state=initial_state,
                baseline_rank_last_checked_at=baseline_rank_last_checked_at,
            )
        finally:
            _ACTIVE_RANK_REFRESH_WATCHES.discard(watch_key)

    async def _poll_rank_refresh(
        self,
        interaction: discord.Interaction,
        *,
        guild_id: int,
        initial_state: AccountProfileState,
        baseline_rank_last_checked_at: datetime | None,
    ) -> None:
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
                if not timestamps_match(
                    link.rank_last_checked_at,
                    baseline_rank_last_checked_at,
                ):
                    await self._edit_profile(interaction, link)
                    return
                presentation = _build_account_profile(self.bot, link)
                await self._edit_presentation(interaction, presentation, link)
                if presentation.state in _ACCOUNT_PROFILE_PENDING_STATES:
                    with suppress(discord.HTTPException):
                        await interaction.followup.send(
                            _ACCOUNT_PROFILE_REFRESH_TIMEOUT_MESSAGE,
                            ephemeral=True,
                        )
                return

            await asyncio.sleep(min(_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS, remaining))
            link = await self.bot.verifications.get_by_user(guild_id, self.owner_id)
            if link is None or not self._matches_current_link(link):
                await self._edit_stale_profile(interaction)
                return

            if not timestamps_match(
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
        _button: discord.ui.Button[AccountProfileView],
    ) -> None:
        if self._refresh_in_progress:
            await interaction.response.send_message(
                "Odświeżanie już się rozpoczęło. Poczekaj chwilę.",
                ephemeral=True,
            )
            return
        await self.run_rank_refresh(interaction, deferred=False)

    async def run_rank_refresh(
        self,
        interaction: discord.Interaction,
        *,
        deferred: bool,
    ) -> None:
        """Queue one manual refresh and keep the original response up to date.

        ``deferred`` is true when the caller already answered the interaction
        with a private thinking response, as the public panel does. That
        response has no profile yet, so every outcome replaces its content.
        """

        button = self.refresh_rank
        self._refresh_in_progress = True
        button.disabled = True
        button.label = REFRESH_RUNNING_BUTTON_LABEL
        button.style = discord.ButtonStyle.secondary
        request_recorded = False
        checking_cooldown = False
        try:
            if not deferred:
                await interaction.response.defer()
            identity = self.link_identity
            if identity is None or identity.puuid is None:
                await self._edit_stale_profile(interaction)
                return
            result = await self.bot.verifications.request_rank_refresh(
                interaction.guild_id or 0,
                self.owner_id,
                cooldown_seconds=(self.bot.settings.rank_refresh_button_cooldown_seconds),
                source="user",
                expected_puuid=identity.puuid,
                expected_platform=identity.platform,
                expected_created_at=identity.created_at,
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
            if not timestamps_match(
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
                    cooldown_message = _rank_refresh_cooldown_message(result.retry_after_seconds)
                    if deferred:
                        await self._edit_presentation(
                            interaction, presentation, link, content=cooldown_message
                        )
                    else:
                        await interaction.followup.send(cooldown_message, ephemeral=True)
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
            await self._report_refresh_failure(
                interaction,
                deferred=deferred,
                request_recorded=request_recorded,
                checking_cooldown=checking_cooldown,
            )

    async def _report_refresh_failure(
        self,
        interaction: discord.Interaction,
        *,
        deferred: bool,
        request_recorded: bool,
        checking_cooldown: bool,
    ) -> None:
        if request_recorded:
            if deferred:
                with suppress(discord.HTTPException):
                    await interaction.edit_original_response(
                        content=_ACCOUNT_PROFILE_REFRESH_OBSERVATION_ERROR_MESSAGE,
                        embed=None,
                        view=None,
                    )
                return
            with suppress(discord.HTTPException):
                await interaction.edit_original_response(view=self)
            with suppress(discord.HTTPException):
                await interaction.followup.send(
                    _ACCOUNT_PROFILE_REFRESH_OBSERVATION_ERROR_MESSAGE,
                    ephemeral=True,
                )
            return
        self._restore_refresh_button(self.refresh_rank)
        failure_message = (
            "Nie udało się sprawdzić, czy możesz odświeżyć rangę. Spróbuj ponownie."
            if checking_cooldown
            else "Nie udało się rozpocząć odświeżania. Spróbuj ponownie."
        )
        if deferred:
            with suppress(discord.HTTPException):
                await interaction.edit_original_response(
                    content=failure_message, embed=None, view=None
                )
            return
        await interaction.followup.send(failure_message, ephemeral=True)

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
            expected_identity=self.link_identity,
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
        identity: VerificationLinkIdentity,
    ) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.owner_id = owner_id
        self.identity = identity

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
                identity=self.identity,
            )
        except Exception:
            logger.exception("Could not remove verification for %s", self.owner_id)
            message = (
                "Nie udało się dokończyć usuwania. Sprawdź aktualny stan w sekcji „Moje konto”."
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
    expected_identity: VerificationLinkIdentity | None = None,
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
    current_identity = VerificationLinkIdentity.from_link(link)
    if expected_identity is not None and (
        current_identity.puuid != expected_identity.puuid
        or current_identity.platform != expected_identity.platform
        or not timestamps_match(current_identity.created_at, expected_identity.created_at)
    ):
        await interaction.response.send_message(
            _ACCOUNT_PROFILE_STALE_MESSAGE,
            ephemeral=True,
        )
        return
    if link.deletion_requested_at is not None:
        await interaction.response.send_message(
            "Usuwanie tego powiązania już trwa.", ephemeral=True
        )
        return
    if link.puuid is None:
        confirmation = (
            "Czy na pewno chcesz usunąć poprzednie powiązanie?\n\n"
            "Rola „Zweryfikowany” zostanie usunięta. "
            "Role regionu i rangi oraz rola „Użytkownik” zostaną.\n"
            "Po usunięciu możesz ponownie połączyć konto Riot."
        )
    else:
        confirmation = (
            "Czy na pewno chcesz usunąć powiązanie z kontem Riot?\n\n"
            "Rola „Zweryfikowany” zostanie usunięta. "
            "Role regionu i rangi oraz rola „Użytkownik” zostaną."
        )
    await interaction.response.send_message(
        confirmation,
        view=DeleteVerificationConfirmationView(
            bot,
            owner_id=interaction.user.id,
            identity=current_identity,
        ),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class AdminDeleteVerificationConfirmationView(discord.ui.View):
    def __init__(
        self,
        bot: MoonPoroBot,
        *,
        owner_id: int,
        guild_id: int,
        target_user_id: int,
        identity: VerificationLinkIdentity,
        reason: str,
    ) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.target_user_id = target_user_id
        self.identity = identity
        self.reason = reason

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Nie możesz potwierdzić operacji rozpoczętej przez inną osobę.",
                ephemeral=True,
            )
            return False
        if interaction.guild_id != self.guild_id or not is_administrator(interaction):
            await interaction.response.send_message(
                "Nie masz już uprawnień do wykonania tej operacji.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Tak, usuń powiązanie",
        style=discord.ButtonStyle.red,
        custom_id="verification:admin-delete:confirm:v1",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[AdminDeleteVerificationConfirmationView],
    ) -> None:
        await interaction.response.edit_message(content="Usuwanie powiązania…", view=None)
        try:
            result = await self.bot.verifications.request_verification_deletion_with_identity(
                self.guild_id,
                self.target_user_id,
                identity=self.identity,
                policy=VerificationDeletionPolicy.ADMIN,
                access_log=VerificationDeletionAccessLog(
                    actor_id=self.owner_id,
                    reason=f"Usunięcie powiązania: {self.reason}",
                ),
            )
            if result.status in {
                VerificationDeletionRequestStatus.LINK_CHANGED,
                VerificationDeletionRequestStatus.NOT_LINKED,
            }:
                message = "Powiązanie się zmieniło. Użyj komendy jeszcze raz."
            elif result.status is VerificationDeletionRequestStatus.ALREADY_REQUESTED:
                message = "Usuwanie tego powiązania już trwa."
            elif result.status is VerificationDeletionRequestStatus.POLICY_CONFLICT:
                message = (
                    "Ta osoba sama zaczęła już usuwać powiązanie. Role regionu i rangi zostaną."
                )
            elif result.link is None:
                message = "Nie udało się rozpocząć usuwania. Spróbuj ponownie."
            else:
                guild = interaction.guild
                if guild is None:
                    guild = self.bot.get_guild(self.guild_id)
                member = guild.get_member(self.target_user_id) if guild is not None else None
                completed = await _process_requested_verification_deletion(
                    self.bot,
                    guild=guild,
                    member=member,
                    link=result.link,
                )
                message = (
                    "Usunięto powiązanie i rolę „Zweryfikowany”. "
                    "Role regionu i rangi oraz rola „Użytkownik” zostały."
                    if completed
                    else "Usuwanie powiązania jeszcze trwa. Bot dokończy je automatycznie."
                )
        except Exception:
            logger.exception("Could not complete administrative verification deletion")
            message = "Nie udało się dokończyć usuwania. Spróbuj ponownie później."
        await interaction.edit_original_response(content=message, view=None)

    @discord.ui.button(
        label="Anuluj",
        style=discord.ButtonStyle.secondary,
        custom_id="verification:admin-delete:cancel:v1",
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[AdminDeleteVerificationConfirmationView],
    ) -> None:
        await interaction.response.edit_message(
            content="Powiązanie nie zostało usunięte.", view=None
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
        # A database/advisory lock may outlive Discord's interaction deadline.
        await interaction.response.defer(ephemeral=True, thinking=True)
        existing_link = await self.bot.verifications.get_by_user(
            interaction.guild.id, interaction.user.id
        )
        if (
            existing_link is not None
            and getattr(existing_link, "deletion_requested_at", None) is not None
        ):
            await interaction.edit_original_response(
                content="Usuwanie poprzedniego powiązania jeszcze trwa. Spróbuj ponownie za chwilę.",
            )
            return
        if existing_link is not None and not getattr(existing_link, "puuid", None):
            await interaction.edit_original_response(
                content="Najpierw usuń poprzednie powiązanie w sekcji „Moje konto”.",
            )
            return
        if (
            existing_link is not None
            and await self.bot.verification_sessions.resume_paused_for_user(
                interaction.guild.id, interaction.user.id
            )
        ):
            await interaction.edit_original_response(
                content="Wznawiamy weryfikację. Bot jeszcze raz spróbuje nadać role."
            )
            return
        already_linked = existing_link is not None
        if not already_linked and member_has_role(
            interaction.user, self.bot.settings.verified_role_name, self.bot.settings
        ):
            # A Verified role without any link is stale. "Moje konto" would show
            # no account to remove, so clear the role and start normally.
            already_linked = not await _clear_orphan_verified_role(self.bot, interaction.user)
        if already_linked:
            await interaction.edit_original_response(
                content="Masz już połączone konto Riot. Aby połączyć inne, najpierw usuń "
                "obecne powiązanie w sekcji „Moje konto”.",
            )
            return

        try:
            session = await self.bot.verification_sessions.create(
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                ttl_seconds=self.bot.settings.rso_session_ttl_seconds,
                discord_username=str(interaction.user),
                discord_guild_name=interaction.guild.name,
            )
        except SessionAlreadyUsed:
            await interaction.edit_original_response(
                content="Łączenie konta już trwa albo się zakończyło. Sprawdź „Moje konto”.",
            )
            return
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
                f"Link jest przypisany do Twojego konta Discord i wygaśnie za {minutes} min. "
                "Nie przekazuj go nikomu."
            ),
            colour=discord.Colour.from_rgb(116, 211, 224),
        )
        embed.set_footer(text="Po zalogowaniu wróć do Discorda, role pojawią się same.")
        await interaction.edit_original_response(content=None, embed=embed, view=link_view)

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
        await _request_rank_refresh_from_panel(
            self.bot,
            interaction,
            start_verification=self.begin_verification,
        )

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
        self.reconcile_verification_audits.start()
        self.verification_maintenance.start()
        self.complete_rso_verifications.start()
        self.report_riot_monitoring.start()

    async def cog_unload(self) -> None:
        self.refresh_verified.cancel()
        self.reconcile_verification_audits.cancel()
        self.verification_maintenance.cancel()
        self.complete_rso_verifications.cancel()
        self.report_riot_monitoring.cancel()

    async def apply_verified_roles(
        self,
        member: discord.Member,
        platform: str,
        leagues: LeagueEntries | None,
    ) -> None:
        self._managed_role_updates.add(member.id)
        try:
            await _apply_verified_roles(self.bot, member, platform, leagues)
        finally:
            self._managed_role_updates.discard(member.id)

    @tasks.loop(seconds=3, reconnect=True)
    async def complete_rso_verifications(self) -> None:
        # tasks.loop stops for good on an unhandled exception, and a database
        # outage raises from every call here. Log it and let the next run retry.
        try:
            await self._complete_pending_verifications()
        except Exception:
            logger.exception("RSO completion worker error; next run will retry")

    async def _complete_pending_verifications(self) -> None:
        # Finish both RSO sessions and accepted icon proofs, so switching the
        # verification mode never strands sessions created by the other mode.
        records = await self.bot.verification_sessions.claim_pending(limit=5, method=None)
        for record in records:
            try:
                await self._complete_rso_verification(record)
            except Exception:
                logger.exception(
                    "Unexpected verification completion error for session %s", record.id
                )
                await self._retry_rso_completion_safely(record, "UNEXPECTED_COMPLETION_ERROR")

    async def _retry_rso_completion_safely(
        self, record: VerificationSession, error_code: str
    ) -> None:
        # One failed retry write must not abort the rest of the batch. A claim
        # left behind is reclaimed by claim_pending once it becomes stale.
        try:
            await self._retry_rso_completion(record, error_code)
        except Exception:
            logger.exception(
                "Could not schedule verification completion retry for session %s", record.id
            )

    async def _reconcile_rso_audits(self) -> None:
        reconciliations = await self.bot.verification_sessions.claim_audit_reconciliations(limit=5)
        for record in reconciliations:
            try:
                await self._publish_rso_audit(record)
            except Exception:
                logger.exception("Could not reconcile RSO audit for session %s", record.id)
                await self._retry_rso_completion_safely(record, "DISCORD_AUDIT_UNAVAILABLE")

    async def _complete_rso_verification(self, record: VerificationSession) -> None:
        platform = record.platform
        puuid = record.puuid
        if platform is None or puuid is None:
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
        cached = _cached_rank_leagues(link)
        cached_checked_at = link.rank_last_checked_at
        role_leagues: LeagueEntries | None
        checked_at: datetime | None
        rank_pending = False
        try:
            if (
                cached_checked_at is not None
                and cached_checked_at >= record.created_at
                and cached is not None
            ):
                role_leagues = cached
                checked_at = cached_checked_at
            else:
                try:
                    leagues = await _get_leagues(self.bot, platform, puuid)
                except RiotAPIUnavailable as error:
                    # A Riot outage can outlast the completion retry budget. Finish
                    # now without guessing a rank; the rank refresh queue adds the
                    # rank role after Riot confirms it.
                    logger.warning(
                        "Riot rank unavailable (HTTP %s) for session %s; "
                        "completing verification without a rank",
                        error.status,
                        record.id,
                    )
                    role_leagues = cached
                    checked_at = cached_checked_at
                    rank_pending = True
                else:
                    recorded = await _record_leagues(
                        self.bot,
                        link,
                        leagues,
                        expected_claimed_at=None,
                    )
                    if recorded is None:
                        await self._retry_rso_completion(record, "RANK_SNAPSHOT_CONFLICT")
                        return
                    role_leagues, checked_at = recorded
            applied = await _run_active_link_role_update(
                self.bot,
                link,
                lambda: self.apply_verified_roles(member, platform, role_leagues),
                expected_rank_last_checked_at=checked_at,
            )
        except VerificationSnapshotChanged:
            await self._retry_rso_completion(record, "RANK_SNAPSHOT_CONFLICT")
            return
        except (discord.HTTPException, TimeoutError):
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
        if not applied:
            await self.bot.verification_sessions.fail_discord(
                record.id, "VERIFICATION_LINK_CHANGED"
            )
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
        if rank_pending:
            # Make the new link due now. An active Riot backoff keeps its own
            # schedule, so the queue does not hammer Riot during the outage.
            await self.bot.verifications.schedule_rank_refresh_now(
                record.guild_id, record.discord_user_id
            )

        try:
            completed = await self._publish_rso_audit(record)
        except (discord.HTTPException, RuntimeError, TimeoutError):
            logger.warning("RSO audit publication will retry for session %s", record.id)
            await self._retry_rso_completion(record, "DISCORD_AUDIT_UNAVAILABLE")
            return
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
        if role_leagues is None:
            message = (
                "Twoje konto Riot jest połączone. "
                f"Na serwerze **{guild.name}** masz już rolę regionu, "
                "a rolę rangi bot doda, gdy Riot ją potwierdzi."
            )
        else:
            message = (
                "Twoje konto Riot jest połączone. "
                f"Na serwerze **{guild.name}** masz już role regionu i rangi."
            )
        with suppress(discord.Forbidden, discord.HTTPException):
            await member.send(message)

    async def _publish_rso_audit(self, record: VerificationSession) -> bool:
        channel_id = record.audit_pending_channel_id or self.bot.settings.zweryfikowani_channel_id
        if channel_id is None:
            raise RuntimeError("Verification audit channel is not configured")

        async def reconcile(current: VerificationSession, active: bool) -> list[int]:
            destination = current.audit_pending_channel_id
            if destination is None:
                raise RuntimeError("Missing pending audit destination")
            channel = self.bot.get_channel(destination)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(destination)
                except discord.NotFound:
                    if active:
                        raise
                    # A deleted destination cannot contain an orphan audit.
                    return []
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                raise RuntimeError("Verification audit channel is unavailable")
            bot_user = self.bot.user
            if bot_user is None or not self.bot.is_ready():
                raise RuntimeError("Discord bot is not ready")
            bot_member = channel.guild.me
            if bot_member is None:
                bot_member = await channel.guild.fetch_member(bot_user.id)
            permissions = channel.permissions_for(bot_member)
            if not permissions.view_channel or not permissions.read_message_history:
                # Discord can return an empty history without this permission.
                # An empty result would lose terminal cleanup intents or send a
                # duplicate after an interrupted publication.
                raise RuntimeError("Verification audit history is not readable")
            profile_icon = current.verification_method == "PROFILE_ICON"
            marker = (
                f"Weryfikacja: ikona profilu #{current.id}"
                if profile_icon
                else f"Weryfikacja RSO #{current.id}"
            )
            message_ids = []
            # A committed intent without a message ID can mean send succeeded
            # immediately before a process/DB failure. Never send blindly on retry.
            try:
                async for message in channel.history(
                    limit=None, after=current.created_at - timedelta(minutes=1), oldest_first=True
                ):
                    if message.author.id == bot_user.id and any(
                        embed.footer.text == marker for embed in message.embeds
                    ):
                        message_ids.append(message.id)
            except discord.NotFound:
                if active:
                    raise
                # The channel may disappear after the cache/fetch lookup.
                return []
            if message_ids or not active:
                return message_ids
            embed = discord.Embed(
                title=(
                    "Weryfikacja konta: ikona profilu"
                    if profile_icon
                    else "Zweryfikowane konto Riot"
                ),
                colour=discord.Colour.green(),
            )
            embed.add_field(
                name="Discord", value=f"<@{current.discord_user_id}> (`{current.discord_user_id}`)"
            )
            if current.platform is None:
                raise RuntimeError("Missing audit platform")
            embed.add_field(name="Region", value=SERVER_TRANSLATION[current.platform])
            if current.riot_game_name and current.riot_tag_line:
                embed.add_field(
                    name="Riot ID",
                    value=f"{current.riot_game_name}#{current.riot_tag_line}",
                    inline=False,
                )
            embed.set_footer(text=marker)
            audit_message = await channel.send(
                embed=embed,
                nonce=f"{'icon' if profile_icon else 'rso'}:{current.id}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return [audit_message.id]

        return await self.bot.verification_sessions.publish_audit(
            record.id, channel_id=channel_id, operation=reconcile
        )

    async def _retry_rso_completion(self, record: VerificationSession, error_code: str) -> None:
        delay = min(5 * (2 ** min(max(record.completion_attempts - 1, 0), 6)), 300)
        paused = await self.bot.verification_sessions.retry_discord(
            record.id,
            error_code=error_code,
            delay_seconds=delay,
            max_attempts=8,
        )
        if paused:
            logger.error(
                "Discord verification completion paused after retry limit for session %s (%s)",
                record.id,
                error_code,
            )

    @tasks.loop(seconds=10, reconnect=True)
    async def reconcile_verification_audits(self) -> None:
        try:
            # Audit history and Discord publication have independent I/O bounds;
            # keep them off the rank worker so unavailable audit channels cannot
            # hold up role synchronization or rank refreshes.
            await self._reconcile_rso_audits()
            await _retry_next_rso_audit_cleanup(self)
        except Exception:
            logger.exception("Verification audit worker error; next run will retry")

    @tasks.loop(seconds=10, reconnect=True)
    async def refresh_verified(self) -> None:
        try:
            await _retry_next_verified_marker_cleanup(self)
            await _retry_next_verification_deletion(self)
            await _retry_next_rank_role_sync(self)
            await _refresh_next_verified(self)
        except Exception:
            logger.exception("Unexpected rank refresh worker error; next run will retry")

    @tasks.loop(minutes=5, reconnect=True)
    async def report_riot_monitoring(self) -> None:
        try:
            await _report_riot_monitoring(self.bot)
        except Exception:
            logger.exception("Riot monitoring report failed; next run will retry")

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

    @reconcile_verification_audits.before_loop
    async def before_audit_reconciliation(self) -> None:
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
        embed = discord.Embed(
            title="Weryfikacja konta League of Legends",
            description=(
                "Zaloguj się przez Riot, a bot nada Ci role regionu i rangi Solo/Duo. "
                "Potem będzie sam dbał o to, żeby ranga była aktualna."
            ),
            colour=discord.Colour.from_rgb(116, 211, 224),
        )
        embed.add_field(
            name="Zasady",
            value=_verification_legal_links(self.bot),
            inline=False,
        )
        embed.set_footer(text="Hasło wpisujesz tylko na stronie Riot.")
        await interaction.response.send_message(embed=embed, view=self.rso_start_view)

    @app_commands.command(
        name="usun_weryfikacje", description="Usuwa powiązanie z Twoim kontem Riot"
    )
    async def remove_own_verification(self, interaction: discord.Interaction) -> None:
        await _show_delete_confirmation(self.bot, interaction)

    @app_commands.command(name="profil", description="Pokazuje dane połączonego konta Riot")
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
    @app_commands.command(name="show_wer_user", description="Szuka konta Discord po Riot ID")
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
                "Sprawdzenie zostało zapisane w rejestrze wyszukiwań.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                "Nie znaleziono powiązania. Sprawdzenie zostało zapisane w rejestrze wyszukiwań.",
                ephemeral=True,
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
                "Nie znaleziono powiązania. Sprawdzenie zostało zapisane w rejestrze wyszukiwań.",
                ephemeral=True,
            )
            return
        region = SERVER_TRANSLATION.get(link.platform, link.platform)
        try:
            account = await riot_api_call(
                lambda: self.bot.riot_client.get_account_v1_by_puuid(
                    puuid=link.puuid, region=API_SERVERS[link.platform]
                ),
                not_found=None,
            )
        except RiotAPIUnavailable:
            await interaction.followup.send("Riot jest chwilowo niedostępny.", ephemeral=True)
            return
        if account is None:
            await interaction.followup.send(
                f"Riot nie znalazł konta przypisanego do tego powiązania (region **{region}**). "
                "Sprawdzenie zostało zapisane w rejestrze wyszukiwań.",
                ephemeral=True,
            )
            return
        riot_id = _riot_id_from_account(account)
        if riot_id is None:
            await interaction.followup.send(
                f"Riot nie zwrócił Riot ID dla tego konta (region **{region}**). "
                "Sprawdzenie zostało zapisane w rejestrze wyszukiwań.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Konto Riot: **{discord.utils.escape_markdown(riot_id)}**, region **{region}**. "
            "Sprawdzenie zostało zapisane w rejestrze wyszukiwań.",
            ephemeral=True,
        )

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(server=_server_choices())
    @app_commands.command(
        name="usun_wer_nick", description="Usuwa powiązanie wybranej osoby z kontem Riot"
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
            reason=f"Sprawdzenie przed usunięciem: {powod}",
            discord_user_id=link.discord_user_id if link else None,
            puuid=account["puuid"],
        )
        if link is None:
            await interaction.followup.send("To konto nie było powiązane.", ephemeral=True)
            return
        if link.deletion_requested_at is not None:
            await interaction.followup.send("Usuwanie tego powiązania już trwa.", ephemeral=True)
            return

        riot_id = discord.utils.escape_markdown(
            _riot_id_from_account(account) or f"{nick.strip()}#{tag.replace('#', '').strip()}"
        )
        region = SERVER_TRANSLATION.get(link.platform, link.platform)
        await interaction.followup.send(
            (
                "Usunąć powiązanie?\n\n"
                f"Discord: <@{link.discord_user_id}> (`{link.discord_user_id}`)\n"
                f"Riot ID: **{riot_id}**\n"
                f"Region: **{region}**\n\n"
                "Bot usunie powiązanie i rolę „Zweryfikowany”.\n"
                "Role regionu i rangi oraz rola „Użytkownik” zostaną."
            ),
            view=AdminDeleteVerificationConfirmationView(
                self.bot,
                owner_id=interaction.user.id,
                guild_id=interaction.guild_id or 0,
                target_user_id=link.discord_user_id,
                identity=VerificationLinkIdentity.from_link(link),
                reason=powod,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(VerificationCog(bot))
