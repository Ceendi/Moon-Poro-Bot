from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from moon_poro.models import (
    GuildFeature,
    ModerationStat,
    VerificationAccessLog,
    VerificationLink,
    Warning,
    WarningModerator,
    WarningStatus,
)


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
        message_id: int,
        platform: str,
        puuid: str,
        method: str = "PROFILE_ICON",
        rank_tier: str | None = None,
        refresh_interval_hours: int = 24,
    ) -> VerificationLink:
        now = datetime.now(UTC)
        link = VerificationLink(
            guild_id=guild_id,
            discord_user_id=user_id,
            message_id=message_id,
            platform=platform,
            puuid=puuid,
            verification_method=method,
            last_known_rank=rank_tier,
            rank_last_checked_at=now if rank_tier is not None else None,
            rank_next_refresh_at=(
                now + timedelta(hours=refresh_interval_hours) if rank_tier is not None else now
            ),
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

    async def complete_rank_refresh(
        self,
        guild_id: int,
        user_id: int,
        *,
        rank_tier: str,
        refresh_interval_hours: int,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None:
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
        max_delay_seconds: int = 21_600,
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
            failures = min(int(link.rank_refresh_failures) + 1, 16)
            link.rank_refresh_failures = failures
            delay: int = min(
                base_delay_seconds * (2 ** (failures - 1)),
                max_delay_seconds,
            )
            link.rank_next_refresh_at = now + timedelta(seconds=delay)
            link.rank_refresh_claimed_at = None
            return delay

    async def defer_rank_refresh(
        self,
        guild_id: int,
        user_id: int,
        *,
        delay_seconds: int,
    ) -> bool:
        next_refresh = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        async with self._sessions.begin() as session:
            link = await session.get(
                VerificationLink,
                (guild_id, user_id),
                with_for_update=True,
            )
            if link is None:
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
            if link is None:
                return False
            if link.rank_next_refresh_at > now:
                link.rank_next_refresh_at = now
            return True

    async def delete_by_user(self, guild_id: int, user_id: int) -> VerificationLink | None:
        async with self._sessions.begin() as session:
            link = await session.get(VerificationLink, (guild_id, user_id))
            if link is not None:
                await session.delete(link)
            return link

    async def delete_by_puuid(self, guild_id: int, puuid: str) -> VerificationLink | None:
        async with self._sessions.begin() as session:
            link = await session.scalar(
                select(VerificationLink).where(
                    VerificationLink.guild_id == guild_id,
                    VerificationLink.puuid == puuid,
                )
            )
            if link is not None:
                await session.delete(link)
            return link

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
                    reason=reason,
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


class WarningRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @staticmethod
    def _with_moderators() -> Any:
        return selectinload(Warning.moderators)

    async def get_active(self, guild_id: int, user_id: int) -> Warning | None:
        async with self._sessions() as session:
            return cast(
                Warning | None,
                await session.scalar(
                    select(Warning)
                    .options(self._with_moderators())
                    .where(
                        Warning.guild_id == guild_id,
                        Warning.discord_user_id == user_id,
                        Warning.status == WarningStatus.ACTIVE.value,
                    )
                ),
            )

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
                parent_id=parent_id,
                moderators=[
                    WarningModerator(moderator_id=value) for value in sorted(moderator_ids)
                ],
            )
            session.add(warning)
            await self._increment_stat(session, guild_id, moderator_id)

        return warning

    async def list_expired(self, guild_id: int) -> list[Warning]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    Warning.status == WarningStatus.ACTIVE.value,
                    Warning.expires_at < datetime.now(UTC),
                )
            )
            return list(result)

    async def list_active(self, guild_id: int) -> list[Warning]:
        async with self._sessions() as session:
            result = await session.scalars(
                select(Warning)
                .options(self._with_moderators())
                .where(
                    Warning.guild_id == guild_id,
                    Warning.status == WarningStatus.ACTIVE.value,
                )
            )
            return list(result)

    async def mark_expired(self, warning_id: int) -> None:
        async with self._sessions.begin() as session:
            warning = await session.get(Warning, warning_id)
            if warning is not None and warning.status == WarningStatus.ACTIVE.value:
                warning.status = WarningStatus.EXPIRED.value

    async def revert(self, guild_id: int, user_id: int) -> tuple[Warning, Warning | None] | None:
        async with self._sessions.begin() as session:
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

            previous = None
            if current.parent_id is not None:
                previous = await session.scalar(
                    select(Warning)
                    .options(self._with_moderators())
                    .where(Warning.id == current.parent_id)
                )
            current.status = WarningStatus.REVOKED.value
            await session.flush()
            if previous is not None:
                if previous.expires_at > datetime.now(UTC):
                    previous.status = WarningStatus.ACTIVE.value
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
