from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased, selectinload

from moon_poro.models import (
    GuildFeature,
    ModerationStat,
    VerificationAccessLog,
    VerificationLink,
    VerificationMarkerCleanup,
    Warning,
    WarningModerator,
    WarningStatus,
)
from moon_poro.outbox import mark_outbox_claimed
from moon_poro.rank_refresh import RankRefreshDecision, RankSnapshot, retry_delay_with_jitter
from moon_poro.time_utils import timestamps_match


class RankRefreshRequestStatus(StrEnum):
    ENQUEUED = "ENQUEUED"
    ALREADY_DUE = "ALREADY_DUE"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    BACKOFF_ACTIVE = "BACKOFF_ACTIVE"
    COOLDOWN = "COOLDOWN"
    LINK_CHANGED = "LINK_CHANGED"
    NOT_LINKED = "NOT_LINKED"


@dataclass(frozen=True, slots=True)
class RankRefreshRequestResult:
    status: RankRefreshRequestStatus
    retry_after_seconds: int | None = None
    baseline_rank_last_checked_at: datetime | None = None


class VerificationDeletionPolicy(StrEnum):
    USER = "user"
    ADMIN = "admin"


class VerificationDeletionRequestStatus(StrEnum):
    REQUESTED = "REQUESTED"
    ALREADY_REQUESTED = "ALREADY_REQUESTED"
    POLICY_CONFLICT = "POLICY_CONFLICT"
    LINK_CHANGED = "LINK_CHANGED"
    NOT_LINKED = "NOT_LINKED"


class VerificationDeletionProcessStatus(StrEnum):
    DELETED = "DELETED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    ALREADY_DELETED = "ALREADY_DELETED"
    LINK_CHANGED = "LINK_CHANGED"
    CLAIM_LOST = "CLAIM_LOST"
    NOT_REQUESTED = "NOT_REQUESTED"


@dataclass(frozen=True, slots=True)
class VerificationLinkIdentity:
    puuid: str | None
    platform: str
    created_at: datetime

    @classmethod
    def from_link(cls, link: VerificationLink) -> VerificationLinkIdentity:
        return cls(
            puuid=link.puuid,
            platform=link.platform,
            created_at=link.created_at,
        )


@dataclass(frozen=True, slots=True)
class VerificationDeletionAccessLog:
    actor_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationDeletionRequestResult:
    status: VerificationDeletionRequestStatus
    link: VerificationLink | None = None


@dataclass(frozen=True, slots=True)
class VerificationDeletionProcessResult:
    status: VerificationDeletionProcessStatus
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class RankRefreshQueueStats:
    due_count: int
    oldest_due_at: datetime | None
    schedule_6h_count: int = 0
    schedule_12h_count: int = 0
    schedule_24h_count: int = 0
    predicted_requests_per_day: float = 0.0
    snapshot_p50_at: datetime | None = None
    snapshot_p95_at: datetime | None = None
    oldest_snapshot_at: datetime | None = None
    tier_changes: int = 0
    counter_resets: int = 0
    unranked_confirmations: int = 0
    pending_role_sync_count: int = 0


class VerificationRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get_by_user(self, guild_id: int, user_id: int) -> VerificationLink | None:
        async with self._sessions() as session:
            return cast(
                VerificationLink | None,
                await session.get(VerificationLink, (guild_id, user_id)),
            )

    async def get_by_puuid(self, guild_id: int, puuid: str) -> VerificationLink | None:
        async with self._sessions() as session:
            return cast(
                VerificationLink | None,
                await session.scalar(
                    select(VerificationLink).where(
                        VerificationLink.guild_id == guild_id,
                        VerificationLink.puuid == puuid,
                    )
                ),
            )

    async def list_for_guild(self, guild_id: int) -> list[VerificationLink]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(VerificationLink).where(VerificationLink.guild_id == guild_id)
            )
            return list(result)

    async def create(
        self,
        *,
        guild_id: int,
        user_id: int,
        message_id: int | None,
        platform: str,
        puuid: str | None,
        audit_channel_id: int | None = None,
        riot_game_name: str | None = None,
        riot_tag_line: str | None = None,
        method: str = "PROFILE_ICON",
        rank_tier: str | None = None,
        rank_snapshot: RankSnapshot | None = None,
        refresh_interval_hours: int = 24,
    ) -> VerificationLink:
        now = datetime.now(UTC)
        snapshot = rank_snapshot or (
            RankSnapshot(tier=rank_tier) if rank_tier is not None else None
        )
        link = VerificationLink(
            guild_id=guild_id,
            discord_user_id=user_id,
            message_id=message_id,
            audit_channel_id=audit_channel_id,
            platform=platform,
            puuid=puuid,
            riot_game_name=riot_game_name,
            riot_tag_line=riot_tag_line,
            verification_method=method,
            last_known_rank=snapshot.tier if snapshot is not None else None,
            last_known_division=snapshot.division if snapshot is not None else None,
            last_known_league_points=(snapshot.league_points if snapshot is not None else None),
            last_known_wins=snapshot.wins if snapshot is not None else None,
            last_known_losses=snapshot.losses if snapshot is not None else None,
            last_known_inactive=snapshot.inactive if snapshot is not None else None,
            rank_last_checked_at=now if snapshot is not None else None,
            rank_schedule_class="24h" if snapshot is not None else None,
            rank_schedule_reason="first_snapshot" if snapshot is not None else None,
            rank_proposed_interval_seconds=(
                refresh_interval_hours * 3600 if snapshot is not None else None
            ),
            rank_next_refresh_at=(
                now + timedelta(hours=refresh_interval_hours) if snapshot is not None else now
            ),
            rank_role_sync_pending=snapshot is not None,
            rank_role_sync_next_attempt_at=now if snapshot is not None else None,
        )
        async with self._sessions.begin() as session:
            session.add(link)
        return link

    async def claim_due_rank_refreshes(
        self,
        guild_id: int,
        *,
        limit: int,
        claim_timeout_seconds: int,
    ) -> list[VerificationLink]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(seconds=claim_timeout_seconds)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationLink)
                .where(
                    VerificationLink.guild_id == guild_id,
                    VerificationLink.puuid.is_not(None),
                    VerificationLink.deletion_requested_at.is_(None),
                    VerificationLink.rank_next_refresh_at <= now,
                    (
                        VerificationLink.rank_refresh_claimed_at.is_(None)
                        | (VerificationLink.rank_refresh_claimed_at <= stale_claim)
                    ),
                )
                .order_by(VerificationLink.rank_next_refresh_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            links = list(result)
            for link in links:
                link.rank_refresh_claimed_at = now
            return links

    async def rank_refresh_queue_stats(self, guild_id: int) -> RankRefreshQueueStats:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            due_row = (
                await session.execute(
                    select(
                        func.count(VerificationLink.discord_user_id),
                        func.min(VerificationLink.rank_next_refresh_at),
                    ).where(
                        VerificationLink.guild_id == guild_id,
                        VerificationLink.puuid.is_not(None),
                        VerificationLink.deletion_requested_at.is_(None),
                        VerificationLink.rank_next_refresh_at <= now,
                    )
                )
            ).one()
            rows = list(
                await session.execute(
                    select(
                        VerificationLink.rank_schedule_class,
                        VerificationLink.rank_proposed_interval_seconds,
                        VerificationLink.rank_last_checked_at,
                        VerificationLink.rank_tier_change_count,
                        VerificationLink.rank_counter_reset_count,
                        VerificationLink.rank_unranked_confirmations,
                        VerificationLink.rank_role_sync_pending,
                    ).where(
                        VerificationLink.guild_id == guild_id,
                        VerificationLink.puuid.is_not(None),
                        VerificationLink.deletion_requested_at.is_(None),
                    )
                )
            )
        class_counts = {"6h": 0, "12h": 0, "24h": 0}
        checked_at: list[datetime] = []
        predicted_requests = 0.0
        tier_changes = 0
        counter_resets = 0
        confirmations = 0
        pending_role_sync = 0
        for row in rows:
            schedule_class = cast(str | None, row[0])
            if schedule_class in class_counts:
                class_counts[schedule_class] += 1
            interval = cast(int | None, row[1]) or 86_400
            predicted_requests += 86_400 / interval
            if row[2] is not None:
                checked_at.append(cast(datetime, row[2]))
            tier_changes += int(row[3])
            counter_resets += int(row[4])
            confirmations += int(int(row[5]) == 1)
            pending_role_sync += int(bool(row[6]))
        checked_at.sort()
        return RankRefreshQueueStats(
            due_count=int(due_row[0]),
            oldest_due_at=cast(datetime | None, due_row[1]),
            schedule_6h_count=class_counts["6h"],
            schedule_12h_count=class_counts["12h"],
            schedule_24h_count=class_counts["24h"],
            predicted_requests_per_day=predicted_requests,
            snapshot_p50_at=_percentile_timestamp(checked_at, 0.50),
            snapshot_p95_at=_percentile_timestamp(checked_at, 0.05),
            oldest_snapshot_at=checked_at[0] if checked_at else None,
            tier_changes=tier_changes,
            counter_resets=counter_resets,
            unranked_confirmations=confirmations,
            pending_role_sync_count=pending_role_sync,
        )

    async def sync_riot_id_if_current(
        self,
        guild_id: int,
        user_id: int,
        *,
        game_name: str,
        tag_line: str,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
        expected_claimed_at: datetime,
    ) -> bool:
        """Update Riot ID only while the caller still owns the rank-refresh claim."""

        normalized_game_name = game_name.strip()[:100]
        normalized_tag_line = tag_line.strip()[:20]
        if not normalized_game_name or not normalized_tag_line:
            return False
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or link.deletion_requested_at is not None
                or link.puuid != expected_puuid
                or link.platform != expected_platform
                or link.created_at != expected_created_at
                or not timestamps_match(link.rank_refresh_claimed_at, expected_claimed_at)
            ):
                return False
            link.riot_game_name = normalized_game_name
            link.riot_tag_line = normalized_tag_line
            return True

    async def record_rank_snapshot(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
        expected_claimed_at: datetime | None,
        expected_rank_last_checked_at: datetime | None,
        decision: RankRefreshDecision,
        next_interval_seconds: int,
    ) -> datetime | None:
        now = datetime.now(UTC)
        snapshot = decision.snapshot
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or link.deletion_requested_at is not None
                or link.puuid != expected_puuid
                or link.platform != expected_platform
                or link.created_at != expected_created_at
                or not timestamps_match(
                    link.rank_last_checked_at,
                    expected_rank_last_checked_at,
                )
                or not timestamps_match(
                    link.rank_refresh_claimed_at,
                    expected_claimed_at,
                )
            ):
                return None
            link.last_known_rank = snapshot.tier
            link.last_known_division = snapshot.division
            link.last_known_league_points = snapshot.league_points
            link.last_known_wins = snapshot.wins
            link.last_known_losses = snapshot.losses
            link.last_known_inactive = snapshot.inactive
            link.rank_last_checked_at = now
            if decision.activity_observed:
                link.rank_last_activity_observed_at = now
            link.rank_schedule_class = decision.schedule_class
            link.rank_schedule_reason = decision.reason
            link.rank_proposed_interval_seconds = decision.interval_seconds
            link.rank_unranked_confirmations = decision.unranked_confirmations
            if decision.tier_changed:
                link.rank_tier_change_count += 1
            if decision.counter_reset:
                link.rank_counter_reset_count += 1
            link.rank_next_refresh_at = now + timedelta(seconds=next_interval_seconds)
            link.rank_refresh_claimed_at = None
            link.rank_refresh_failures = 0
            link.rank_role_sync_pending = True
            link.rank_role_sync_claimed_at = None
            link.rank_role_sync_next_attempt_at = now
            link.rank_role_sync_failures = 0
            return now

    async def complete_rank_refresh(
        self,
        guild_id: int,
        user_id: int,
        *,
        rank_tier: str,
        refresh_interval_hours: int,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
        expected_claimed_at: datetime,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or not _link_identity_matches(
                    link,
                    expected_puuid=expected_puuid,
                    expected_platform=expected_platform,
                    expected_created_at=expected_created_at,
                )
                or not timestamps_match(
                    link.rank_refresh_claimed_at,
                    expected_claimed_at,
                )
            ):
                return False
            link.last_known_rank = rank_tier
            link.rank_last_checked_at = now
            link.rank_next_refresh_at = now + timedelta(hours=refresh_interval_hours)
            link.rank_refresh_claimed_at = None
            link.rank_refresh_failures = 0
            return True

    async def retry_rank_refresh(
        self,
        guild_id: int,
        user_id: int,
        *,
        base_delay_seconds: int,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
        expected_claimed_at: datetime,
        max_delay_seconds: int = 21_600,
    ) -> int | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or not _link_identity_matches(
                    link,
                    expected_puuid=expected_puuid,
                    expected_platform=expected_platform,
                    expected_created_at=expected_created_at,
                )
                or not timestamps_match(
                    link.rank_refresh_claimed_at,
                    expected_claimed_at,
                )
            ):
                return None
            failures = min(int(link.rank_refresh_failures) + 1, 16)
            link.rank_refresh_failures = failures
            delay = retry_delay_with_jitter(
                base_delay_seconds,
                failures,
                guild_id=guild_id,
                user_id=user_id,
                max_delay_seconds=max_delay_seconds,
            )
            link.rank_next_refresh_at = now + timedelta(seconds=delay)
            link.rank_refresh_claimed_at = None
            return delay

    async def release_rank_refresh_claim(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
        expected_claimed_at: datetime,
    ) -> bool:
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or not _link_identity_matches(
                    link,
                    expected_puuid=expected_puuid,
                    expected_platform=expected_platform,
                    expected_created_at=expected_created_at,
                )
                or not timestamps_match(
                    link.rank_refresh_claimed_at,
                    expected_claimed_at,
                )
            ):
                return False
            link.rank_refresh_claimed_at = None
            return True

    async def defer_rank_refresh(
        self,
        guild_id: int,
        user_id: int,
        *,
        delay_seconds: int,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
        expected_claimed_at: datetime,
    ) -> bool:
        next_refresh = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or not _link_identity_matches(
                    link,
                    expected_puuid=expected_puuid,
                    expected_platform=expected_platform,
                    expected_created_at=expected_created_at,
                )
                or not timestamps_match(
                    link.rank_refresh_claimed_at,
                    expected_claimed_at,
                )
            ):
                return False
            link.rank_next_refresh_at = next_refresh
            link.rank_refresh_claimed_at = None
            return True

    async def schedule_rank_refresh_now(self, guild_id: int, user_id: int) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None or link.deletion_requested_at is not None:
                return False
            if link.rank_refresh_claimed_at is not None or link.rank_refresh_failures > 0:
                return True
            if link.rank_next_refresh_at > now:
                link.rank_next_refresh_at = now
            return True

    async def request_rank_refresh(
        self,
        guild_id: int,
        user_id: int,
        *,
        cooldown_seconds: int,
        source: str,
        expected_puuid: str | None = None,
        expected_platform: str | None = None,
        expected_created_at: datetime | None = None,
    ) -> RankRefreshRequestResult:
        if source not in {"user", "role_tamper"}:
            raise ValueError("source must be user or role_tamper")
        now = datetime.now(UTC)
        timestamp_name = (
            "rank_user_refresh_requested_at"
            if source == "user"
            else "rank_manual_refresh_requested_at"
        )
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None or link.puuid is None or link.deletion_requested_at is not None:
                return RankRefreshRequestResult(RankRefreshRequestStatus.NOT_LINKED)
            if not _link_identity_matches(
                link,
                expected_puuid=expected_puuid,
                expected_platform=expected_platform,
                expected_created_at=expected_created_at,
            ):
                return RankRefreshRequestResult(RankRefreshRequestStatus.LINK_CHANGED)
            baseline_rank_last_checked_at = link.rank_last_checked_at
            requested_at = cast(datetime | None, getattr(link, timestamp_name))
            if requested_at is not None:
                retry_after = cooldown_seconds - int((now - requested_at).total_seconds())
                if retry_after > 0:
                    return RankRefreshRequestResult(
                        RankRefreshRequestStatus.COOLDOWN,
                        retry_after_seconds=retry_after,
                        baseline_rank_last_checked_at=baseline_rank_last_checked_at,
                    )
            setattr(link, timestamp_name, now)
            if link.rank_refresh_claimed_at is not None:
                return RankRefreshRequestResult(
                    RankRefreshRequestStatus.ALREADY_CLAIMED,
                    baseline_rank_last_checked_at=baseline_rank_last_checked_at,
                )
            if link.rank_next_refresh_at <= now:
                return RankRefreshRequestResult(
                    RankRefreshRequestStatus.ALREADY_DUE,
                    baseline_rank_last_checked_at=baseline_rank_last_checked_at,
                )
            if link.rank_refresh_failures > 0:
                return RankRefreshRequestResult(
                    RankRefreshRequestStatus.BACKOFF_ACTIVE,
                    baseline_rank_last_checked_at=baseline_rank_last_checked_at,
                )
            link.rank_next_refresh_at = now
            return RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=baseline_rank_last_checked_at,
            )

    async def claim_due_rank_role_syncs(
        self,
        guild_id: int,
        *,
        limit: int,
        claim_timeout_seconds: int,
    ) -> list[VerificationLink]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(seconds=claim_timeout_seconds)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationLink)
                .where(
                    VerificationLink.guild_id == guild_id,
                    VerificationLink.puuid.is_not(None),
                    VerificationLink.deletion_requested_at.is_(None),
                    VerificationLink.rank_role_sync_pending.is_(True),
                    VerificationLink.rank_role_sync_next_attempt_at <= now,
                    (
                        VerificationLink.rank_role_sync_claimed_at.is_(None)
                        | (VerificationLink.rank_role_sync_claimed_at <= stale_claim)
                    ),
                )
                .order_by(VerificationLink.rank_role_sync_next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            links = list(result)
            for link in links:
                link.rank_role_sync_claimed_at = now
            return links

    async def acknowledge_rank_role_sync(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_rank_last_checked_at: datetime | None = None,
        expected_puuid: str | None = None,
        expected_platform: str | None = None,
        expected_created_at: datetime | None = None,
    ) -> bool:
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None:
                return False
            if not _link_identity_matches(
                link,
                expected_puuid=expected_puuid,
                expected_platform=expected_platform,
                expected_created_at=expected_created_at,
            ):
                return False
            if (
                expected_rank_last_checked_at is not None
                and link.rank_last_checked_at != expected_rank_last_checked_at
            ):
                return False
            link.rank_role_sync_pending = False
            link.rank_role_sync_claimed_at = None
            link.rank_role_sync_next_attempt_at = None
            link.rank_role_sync_failures = 0
            return True

    async def retry_rank_role_sync(
        self,
        guild_id: int,
        user_id: int,
        *,
        base_delay_seconds: int,
        max_delay_seconds: int = 21_600,
        expected_rank_last_checked_at: datetime | None = None,
        expected_puuid: str | None = None,
        expected_platform: str | None = None,
        expected_created_at: datetime | None = None,
    ) -> int | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None:
                return None
            if not _link_identity_matches(
                link,
                expected_puuid=expected_puuid,
                expected_platform=expected_platform,
                expected_created_at=expected_created_at,
            ):
                return None
            if (
                expected_rank_last_checked_at is not None
                and link.rank_last_checked_at != expected_rank_last_checked_at
            ):
                return None
            failures = min(link.rank_role_sync_failures + 1, 16)
            delay = retry_delay_with_jitter(
                base_delay_seconds,
                failures,
                guild_id=guild_id,
                user_id=user_id,
                max_delay_seconds=max_delay_seconds,
            )
            link.rank_role_sync_pending = True
            link.rank_role_sync_claimed_at = None
            link.rank_role_sync_failures = failures
            link.rank_role_sync_next_attempt_at = now + timedelta(seconds=delay)
            return delay

    async def defer_rank_role_sync(
        self,
        guild_id: int,
        user_id: int,
        *,
        delay_seconds: int,
        expected_rank_last_checked_at: datetime | None,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
    ) -> bool:
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None or not _link_identity_matches(
                link,
                expected_puuid=expected_puuid,
                expected_platform=expected_platform,
                expected_created_at=expected_created_at,
            ):
                return False
            if link.rank_last_checked_at != expected_rank_last_checked_at:
                return False
            link.rank_role_sync_pending = True
            link.rank_role_sync_claimed_at = None
            link.rank_role_sync_next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=delay_seconds
            )
            return True

    async def is_current_verification(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_puuid: str,
        expected_platform: str,
        expected_created_at: datetime,
    ) -> bool:
        async with self._sessions() as session:
            link = await session.get(VerificationLink, (guild_id, user_id))
            return link is not None and _link_identity_matches(
                link,
                expected_puuid=expected_puuid,
                expected_platform=expected_platform,
                expected_created_at=expected_created_at,
            )

    async def request_verification_deletion_with_identity(
        self,
        guild_id: int,
        user_id: int,
        *,
        identity: VerificationLinkIdentity,
        policy: VerificationDeletionPolicy,
        access_log: VerificationDeletionAccessLog | None = None,
    ) -> VerificationDeletionRequestResult:
        policy = VerificationDeletionPolicy(policy)
        if policy is VerificationDeletionPolicy.ADMIN:
            if access_log is None:
                raise ValueError("admin deletion requires an access log")
            access_reason = _verification_access_reason(access_log.reason)
        else:
            if access_log is not None:
                raise ValueError("user deletion cannot include an admin access log")
            access_reason = None

        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None:
                return VerificationDeletionRequestResult(
                    VerificationDeletionRequestStatus.NOT_LINKED
                )
            if not _strict_link_identity_matches(link, identity):
                return VerificationDeletionRequestResult(
                    VerificationDeletionRequestStatus.LINK_CHANGED
                )
            remove_rank_region_roles = policy is VerificationDeletionPolicy.ADMIN
            if link.deletion_requested_at is not None:
                status = (
                    VerificationDeletionRequestStatus.ALREADY_REQUESTED
                    if link.deletion_remove_rank_region_roles == remove_rank_region_roles
                    else VerificationDeletionRequestStatus.POLICY_CONFLICT
                )
                return VerificationDeletionRequestResult(status, link)

            link.deletion_requested_at = now
            link.deletion_claimed_at = now
            link.deletion_next_attempt_at = now
            link.deletion_failures = 0
            link.deletion_remove_rank_region_roles = remove_rank_region_roles
            link.rank_refresh_claimed_at = None
            link.rank_role_sync_pending = False
            link.rank_role_sync_claimed_at = None
            link.rank_role_sync_next_attempt_at = None
            if access_log is not None:
                session.add(
                    VerificationAccessLog(
                        guild_id=guild_id,
                        actor_id=access_log.actor_id,
                        reason=cast(str, access_reason),
                        discord_user_id=link.discord_user_id,
                        puuid=link.puuid,
                    )
                )
            return VerificationDeletionRequestResult(
                VerificationDeletionRequestStatus.REQUESTED,
                link,
            )

    async def enqueue_verified_marker_cleanup(self, guild_id: int, user_id: int) -> int:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(
                VerificationMarkerCleanup,
                (guild_id, user_id),
                with_for_update=True,
            )
            if record is None:
                session.add(
                    VerificationMarkerCleanup(
                        guild_id=guild_id,
                        discord_user_id=user_id,
                        next_attempt_at=now,
                        generation=1,
                        created_at=now,
                    )
                )
                return 1
            record.generation += 1
            record.failures = 0
            record.claimed_at = None
            record.next_attempt_at = now
            return record.generation

    async def claim_due_verified_marker_cleanups(
        self,
        guild_id: int,
        *,
        limit: int,
        claim_timeout_seconds: int,
    ) -> list[VerificationMarkerCleanup]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(seconds=claim_timeout_seconds)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationMarkerCleanup)
                .where(
                    VerificationMarkerCleanup.guild_id == guild_id,
                    VerificationMarkerCleanup.next_attempt_at <= now,
                    (
                        VerificationMarkerCleanup.claimed_at.is_(None)
                        | (VerificationMarkerCleanup.claimed_at <= stale_claim)
                    ),
                )
                .order_by(VerificationMarkerCleanup.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            return mark_outbox_claimed(result, now)

    async def acknowledge_verified_marker_cleanup(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_generation: int,
    ) -> bool:
        async with self._sessions.begin() as session:
            current_generation = await session.scalar(
                select(VerificationMarkerCleanup.generation)
                .where(
                    VerificationMarkerCleanup.guild_id == guild_id,
                    VerificationMarkerCleanup.discord_user_id == user_id,
                )
                .with_for_update()
            )
            if current_generation is None:
                return True
            if current_generation != expected_generation:
                return False
            removed = await session.execute(
                delete(VerificationMarkerCleanup).where(
                    VerificationMarkerCleanup.guild_id == guild_id,
                    VerificationMarkerCleanup.discord_user_id == user_id,
                )
            )
            return (cast(CursorResult[Any], removed).rowcount or 0) == 1

    async def retry_verified_marker_cleanup(
        self,
        guild_id: int,
        user_id: int,
        *,
        expected_generation: int,
        base_delay_seconds: int,
        max_delay_seconds: int = 21_600,
    ) -> int | None:
        now = datetime.now(UTC)
        retry_delay: int | None = None
        async with self._sessions.begin() as session:
            record = await session.get(
                VerificationMarkerCleanup,
                (guild_id, user_id),
                with_for_update=True,
            )
            if record is None or record.generation != expected_generation:
                return None
            failures = min(record.failures + 1, 16)
            delay = retry_delay_with_jitter(
                base_delay_seconds,
                failures,
                guild_id=guild_id,
                user_id=user_id,
                max_delay_seconds=max_delay_seconds,
            )
            record.next_attempt_at = now + timedelta(seconds=delay)
            record.claimed_at = None
            record.failures = failures
            retry_delay = delay
        return retry_delay

    async def claim_due_verification_deletions(
        self,
        guild_id: int,
        *,
        limit: int,
        claim_timeout_seconds: int,
    ) -> list[VerificationLink]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(seconds=claim_timeout_seconds)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationLink)
                .where(
                    VerificationLink.guild_id == guild_id,
                    VerificationLink.deletion_requested_at.is_not(None),
                    VerificationLink.deletion_next_attempt_at <= now,
                    (
                        VerificationLink.deletion_claimed_at.is_(None)
                        | (VerificationLink.deletion_claimed_at <= stale_claim)
                    ),
                )
                .order_by(VerificationLink.deletion_next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            links = list(result)
            for link in links:
                link.deletion_claimed_at = now
            return links

    async def run_verification_role_update_with_identity(
        self,
        guild_id: int,
        user_id: int,
        *,
        identity: VerificationLinkIdentity,
        operation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Apply Discord roles only while the observed link is still active.

        ``operation`` must perform Discord I/O only; calling back into a repository
        here would risk a lock-order deadlock while this link row is locked.
        """

        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or link.deletion_requested_at is not None
                or not _strict_link_identity_matches(link, identity)
            ):
                return False
            await operation()
            return True

    async def run_verification_deletion_role_cleanup_with_identity(
        self,
        guild_id: int,
        user_id: int,
        *,
        identity: VerificationLinkIdentity,
        expected_requested_at: datetime,
        expected_remove_rank_region_roles: bool,
        operation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Fence Discord role cleanup to the observed pending deletion.

        ``operation`` must perform Discord I/O only. Keeping the link row locked
        until it finishes prevents an old join/member-update event from removing
        roles after deletion completed and the user connected a new Riot account.
        """

        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if (
                link is None
                or not _strict_link_identity_matches(link, identity)
                or not timestamps_match(link.deletion_requested_at, expected_requested_at)
                or link.deletion_remove_rank_region_roles != expected_remove_rank_region_roles
            ):
                return False
            await operation()
            return True

    async def process_verification_deletion_with_identity(
        self,
        guild_id: int,
        user_id: int,
        *,
        identity: VerificationLinkIdentity,
        expected_claimed_at: datetime,
        base_delay_seconds: int,
        operation: Callable[[], Awaitable[bool]],
    ) -> VerificationDeletionProcessResult:
        """Run Discord cleanup and finalize one strictly identified deletion claim.

        ``operation`` must perform Discord I/O only; calling back into a repository
        here would risk a lock-order deadlock while this link row is locked.
        """

        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None:
                return VerificationDeletionProcessResult(
                    VerificationDeletionProcessStatus.ALREADY_DELETED
                )
            if not _strict_link_identity_matches(link, identity):
                return VerificationDeletionProcessResult(
                    VerificationDeletionProcessStatus.LINK_CHANGED
                )
            if link.deletion_requested_at is None:
                return VerificationDeletionProcessResult(
                    VerificationDeletionProcessStatus.NOT_REQUESTED
                )
            if not timestamps_match(link.deletion_claimed_at, expected_claimed_at):
                return VerificationDeletionProcessResult(
                    VerificationDeletionProcessStatus.CLAIM_LOST
                )

            if await operation():
                await session.delete(link)
                return VerificationDeletionProcessResult(VerificationDeletionProcessStatus.DELETED)

            failures = min(link.deletion_failures + 1, 16)
            delay = retry_delay_with_jitter(
                base_delay_seconds,
                failures,
                guild_id=guild_id,
                user_id=user_id,
            )
            link.deletion_failures = failures
            link.deletion_claimed_at = None
            link.deletion_next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            return VerificationDeletionProcessResult(
                VerificationDeletionProcessStatus.RETRY_SCHEDULED,
                retry_after_seconds=delay,
            )

    async def log_access(
        self,
        *,
        guild_id: int,
        actor_id: int,
        reason: str,
        discord_user_id: int | None,
        puuid: str | None,
    ) -> None:
        async with self._sessions.begin() as session:
            session.add(
                VerificationAccessLog(
                    guild_id=guild_id,
                    actor_id=actor_id,
                    reason=_verification_access_reason(reason),
                    discord_user_id=discord_user_id,
                    puuid=puuid,
                )
            )

    async def purge_access_logs(self, guild_id: int, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(VerificationAccessLog).where(
                    VerificationAccessLog.guild_id == guild_id,
                    VerificationAccessLog.created_at < cutoff,
                )
            )
            return cast(CursorResult[Any], result).rowcount or 0


def _percentile_timestamp(values: list[datetime], percentile: float) -> datetime | None:
    if not values:
        return None
    index = round((len(values) - 1) * percentile)
    return values[index]


def _link_identity_matches(
    link: VerificationLink,
    *,
    expected_puuid: str | None,
    expected_platform: str | None,
    expected_created_at: datetime | None,
    allow_deleting: bool = False,
) -> bool:
    if link.deletion_requested_at is not None and not allow_deleting:
        return False
    return (
        (expected_puuid is None or link.puuid == expected_puuid)
        and (expected_platform is None or link.platform == expected_platform)
        and (expected_created_at is None or link.created_at == expected_created_at)
    )


def _strict_link_identity_matches(
    link: VerificationLink,
    identity: VerificationLinkIdentity,
) -> bool:
    return (
        link.puuid == identity.puuid
        and link.platform == identity.platform
        and timestamps_match(link.created_at, identity.created_at)
    )


def _verification_access_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        raise ValueError("access log reason cannot be empty")
    return normalized[:300]


class WarningRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @staticmethod
    def _with_moderators() -> Any:
        return selectinload(Warning.moderators)

    @staticmethod
    def _member_lock_key(guild_id: int, user_id: int) -> int:
        value = f"moon-poro-warning:{guild_id}:{user_id}".encode()
        return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), signed=True)

    async def _lock_member(self, session: AsyncSession, guild_id: int, user_id: int) -> None:
        """Serialize warning state changes, including creation when no row exists."""

        await session.execute(
            select(func.pg_advisory_xact_lock(self._member_lock_key(guild_id, user_id)))
        )

    @staticmethod
    def _expire(warning: Warning) -> None:
        warning.status = WarningStatus.EXPIRED.value
        warning.role_sync_pending = True
        warning.audit_sync_pending = True

    async def get_active(self, guild_id: int, user_id: int) -> Warning | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await self._lock_member(session, guild_id, user_id)
            active = cast(
                Warning | None,
                await session.scalar(
                    select(Warning)
                    .options(self._with_moderators())
                    .where(
                        Warning.guild_id == guild_id,
                        Warning.discord_user_id == user_id,
                        Warning.status == WarningStatus.ACTIVE.value,
                    )
                    .with_for_update()
                ),
            )
            if active is not None and active.expires_at <= now:
                self._expire(active)
                return None
            return active

    async def issue(
        self,
        *,
        guild_id: int,
        user_id: int,
        requested_level: int,
        reasons: str,
        description: str | None,
        moderator_id: int,
        message_id: int,
        duration_by_level: dict[int, int],
    ) -> Warning:
        now = datetime.now(UTC).replace(microsecond=0)
        async with self._sessions.begin() as session:
            await self._lock_member(session, guild_id, user_id)
            active = await session.scalar(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    Warning.discord_user_id == user_id,
                    Warning.status == WarningStatus.ACTIVE.value,
                )
                .with_for_update()
            )

            if active is not None and active.expires_at <= now:
                self._expire(active)
                await session.flush()
                active = None

            if active is None:
                level = requested_level
                starts_at = now
                combined_reasons = reasons
                combined_description = description
                moderator_ids = {moderator_id}
                parent_id = None
            else:
                level = min(active.level + requested_level, 3)
                starts_at = active.starts_at
                combined_reasons = f"{active.reasons}/{reasons}"
                descriptions = [value for value in (active.description, description) if value]
                combined_description = "\n".join(descriptions) or None
                moderator_ids = {item.moderator_id for item in active.moderators} | {moderator_id}
                parent_id = active.id
                active.status = WarningStatus.SUPERSEDED.value
                active.role_sync_pending = False
                active.audit_sync_pending = False
                await session.flush()

            warning = Warning(
                guild_id=guild_id,
                discord_user_id=user_id,
                level=level,
                reasons=combined_reasons,
                description=combined_description,
                starts_at=starts_at,
                expires_at=now + timedelta(days=duration_by_level[level]),
                message_id=message_id,
                status=WarningStatus.ACTIVE.value,
                role_sync_pending=True,
                audit_sync_pending=True,
                parent_id=parent_id,
                moderators=[
                    WarningModerator(moderator_id=value) for value in sorted(moderator_ids)
                ],
            )
            session.add(warning)
            await self._increment_stat(session, guild_id, moderator_id)

        return warning

    async def expire_due(self, guild_id: int) -> list[Warning]:
        """Atomically make every elapsed warning logically inactive."""

        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    Warning.status == WarningStatus.ACTIVE.value,
                    Warning.expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
            warnings = list(result)
            for warning in warnings:
                self._expire(warning)
            return warnings

    async def list_active(self, guild_id: int) -> list[Warning]:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.scalars(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    Warning.status == WarningStatus.ACTIVE.value,
                    Warning.expires_at > now,
                )
            )
            return list(result)

    async def list_for_reconciliation(self, guild_id: int) -> list[Warning]:
        pending_audit = aliased(Warning)
        pending_message_ids = select(pending_audit.message_id).where(
            pending_audit.guild_id == guild_id,
            pending_audit.audit_sync_pending.is_(True),
        )
        async with self._sessions() as session:
            result = await session.scalars(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    or_(
                        Warning.status == WarningStatus.ACTIVE.value,
                        Warning.role_sync_pending.is_(True),
                        Warning.audit_sync_pending.is_(True),
                        Warning.message_id.in_(pending_message_ids),
                    ),
                )
                .order_by(Warning.id)
            )
            return list(result)

    async def acknowledge_role_sync(self, guild_id: int, user_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(Warning)
                .where(
                    Warning.guild_id == guild_id,
                    Warning.discord_user_id == user_id,
                    Warning.role_sync_pending.is_(True),
                )
                .values(role_sync_pending=False)
            )

    async def acknowledge_audit_sync(
        self,
        guild_id: int,
        message_id: int,
        through_warning_id: int,
    ) -> None:
        """Acknowledge only revisions covered by the rendered audit snapshot."""

        async with self._sessions.begin() as session:
            await session.execute(
                update(Warning)
                .where(
                    Warning.guild_id == guild_id,
                    Warning.message_id == message_id,
                    Warning.id <= through_warning_id,
                    Warning.audit_sync_pending.is_(True),
                )
                .values(audit_sync_pending=False)
            )

    async def revert(self, guild_id: int, user_id: int) -> tuple[Warning, Warning | None] | None:
        async with self._sessions.begin() as session:
            await self._lock_member(session, guild_id, user_id)
            current = await session.scalar(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    Warning.discord_user_id == user_id,
                    Warning.status == WarningStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if current is None:
                return None
            now = datetime.now(UTC)
            if current.expires_at <= now:
                self._expire(current)
                return None

            previous = None
            if current.parent_id is not None:
                previous = await session.scalar(
                    select(Warning)
                    .options(self._with_moderators())
                    .where(Warning.id == current.parent_id)
                )
            current.status = WarningStatus.REVOKED.value
            current.role_sync_pending = True
            current.audit_sync_pending = True
            await session.flush()
            if previous is not None:
                if previous.expires_at > now:
                    previous.status = WarningStatus.ACTIVE.value
                    previous.audit_sync_pending = True
                    current.audit_sync_pending = False
                else:
                    previous.status = WarningStatus.EXPIRED.value
                    previous = None
            return current, previous

    @staticmethod
    async def _increment_stat(session: AsyncSession, guild_id: int, moderator_id: int) -> None:
        now = datetime.now(UTC)
        statement = insert(ModerationStat).values(
            guild_id=guild_id,
            moderator_id=moderator_id,
            year=now.year,
            month=now.month,
            warnings_count=1,
            reports_count=0,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["guild_id", "moderator_id", "year", "month"],
            set_={"warnings_count": ModerationStat.warnings_count + 1},
        )
        await session.execute(statement)


class ModerationStatsRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def list_for_guild(self, guild_id: int) -> list[ModerationStat]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(ModerationStat)
                .where(ModerationStat.guild_id == guild_id)
                .order_by(ModerationStat.year, ModerationStat.month, ModerationStat.moderator_id)
            )
            return list(result)


class GuildFeatureRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(self, guild_id: int, key: str, default: bool) -> bool:
        async with self._sessions() as session:
            feature = await session.get(GuildFeature, (guild_id, key))
            return default if feature is None else feature.enabled

    async def set(self, guild_id: int, key: str, enabled: bool, actor_id: int) -> None:
        statement = insert(GuildFeature).values(
            guild_id=guild_id,
            feature_key=key,
            enabled=enabled,
            updated_by=actor_id,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["guild_id", "feature_key"],
            set_={"enabled": enabled, "updated_by": actor_id, "updated_at": datetime.now(UTC)},
        )
        async with self._sessions.begin() as session:
            await session.execute(statement)
