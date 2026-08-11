from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moon_poro.models import (
    VerificationLink,
    VerificationSession,
    VerificationSessionStatus,
)

ACTIVE_SESSION_STATUSES = (
    VerificationSessionStatus.CREATED.value,
    VerificationSessionStatus.AWAITING_RIOT.value,
    VerificationSessionStatus.PROCESSING_RIOT.value,
    VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value,
    VerificationSessionStatus.APPLYING_DISCORD.value,
)
EXPIRABLE_SESSION_STATUSES = (
    VerificationSessionStatus.CREATED.value,
    VerificationSessionStatus.AWAITING_RIOT.value,
    VerificationSessionStatus.PROCESSING_RIOT.value,
)
TERMINAL_SESSION_STATUSES = (
    VerificationSessionStatus.COMPLETED.value,
    VerificationSessionStatus.FAILED.value,
    VerificationSessionStatus.EXPIRED.value,
    VerificationSessionStatus.CANCELLED.value,
)


class SessionError(RuntimeError):
    pass


class SessionNotFound(SessionError):
    pass


class SessionExpired(SessionError):
    pass


class SessionAlreadyUsed(SessionError):
    pass


class LinkReservationResult(StrEnum):
    RESERVED = "RESERVED"
    DISCORD_ALREADY_LINKED = "DISCORD_ALREADY_LINKED"
    RIOT_ALREADY_LINKED = "RIOT_ALREADY_LINKED"


@dataclass(frozen=True, slots=True)
class CreatedVerificationSession:
    token: str
    expires_at: datetime


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def as_utc(value: datetime) -> datetime:
    """Normalize drivers such as SQLite that drop timezone metadata on round-trip."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class VerificationSessionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(
        self, *, guild_id: int, user_id: int, ttl_seconds: int
    ) -> CreatedVerificationSession:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        record = VerificationSession(
            guild_id=guild_id,
            discord_user_id=user_id,
            start_token_hash=hash_secret(token),
            status=VerificationSessionStatus.CREATED.value,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            updated_at=now,
        )
        async with self._sessions.begin() as session:
            await session.execute(
                update(VerificationSession)
                .where(
                    VerificationSession.guild_id == guild_id,
                    VerificationSession.discord_user_id == user_id,
                    VerificationSession.status.in_(ACTIVE_SESSION_STATUSES),
                )
                .values(
                    status=VerificationSessionStatus.CANCELLED.value,
                    error_code="SUPERSEDED",
                    updated_at=now,
                    completed_at=now,
                )
            )
            session.add(record)
        return CreatedVerificationSession(token=token, expires_at=record.expires_at)

    async def get_by_start_token(self, token: str) -> VerificationSession | None:
        token_hash = hash_secret(token)
        async with self._sessions() as session:
            return cast(
                VerificationSession | None,
                await session.scalar(
                    select(VerificationSession).where(
                        VerificationSession.start_token_hash == token_hash
                    )
                ),
            )

    async def begin_oauth(self, *, token: str, state: str) -> VerificationSession:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.scalar(
                select(VerificationSession)
                .where(VerificationSession.start_token_hash == hash_secret(token))
                .with_for_update()
            )
            if record is None:
                raise SessionNotFound
            if as_utc(record.expires_at) <= now:
                record.status = VerificationSessionStatus.EXPIRED.value
                record.error_code = "SESSION_EXPIRED"
                record.updated_at = now
                record.completed_at = now
                raise SessionExpired
            if record.status != VerificationSessionStatus.CREATED.value:
                raise SessionAlreadyUsed
            record.oauth_state_hash = hash_secret(state)
            record.status = VerificationSessionStatus.AWAITING_RIOT.value
            record.updated_at = now
            return record

    async def claim_callback(self, state: str) -> VerificationSession:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.scalar(
                select(VerificationSession)
                .where(VerificationSession.oauth_state_hash == hash_secret(state))
                .with_for_update()
            )
            if record is None:
                raise SessionNotFound
            if as_utc(record.expires_at) <= now:
                record.status = VerificationSessionStatus.EXPIRED.value
                record.error_code = "SESSION_EXPIRED"
                record.updated_at = now
                record.completed_at = now
                raise SessionExpired
            if record.status != VerificationSessionStatus.AWAITING_RIOT.value:
                raise SessionAlreadyUsed
            record.status = VerificationSessionStatus.PROCESSING_RIOT.value
            record.updated_at = now
            return record

    async def reserve_link(
        self,
        *,
        session_id: int,
        platform: str,
        puuid: str,
        game_name: str,
        tag_line: str,
    ) -> LinkReservationResult:
        now = datetime.now(UTC)
        try:
            async with self._sessions.begin() as session:
                record = await session.scalar(
                    select(VerificationSession)
                    .where(VerificationSession.id == session_id)
                    .with_for_update()
                )
                if record is None:
                    raise SessionNotFound
                if record.status != VerificationSessionStatus.PROCESSING_RIOT.value:
                    raise SessionAlreadyUsed

                by_user = await session.get(
                    VerificationLink,
                    (record.guild_id, record.discord_user_id),
                    with_for_update=True,
                )
                if by_user is not None:
                    return await self._fail_reservation(
                        record, LinkReservationResult.DISCORD_ALREADY_LINKED, now
                    )
                by_puuid = await session.scalar(
                    select(VerificationLink)
                    .where(
                        VerificationLink.guild_id == record.guild_id,
                        VerificationLink.puuid == puuid,
                    )
                    .with_for_update()
                )
                if by_puuid is not None:
                    return await self._fail_reservation(
                        record, LinkReservationResult.RIOT_ALREADY_LINKED, now
                    )

                session.add(
                    VerificationLink(
                        guild_id=record.guild_id,
                        discord_user_id=record.discord_user_id,
                        message_id=None,
                        platform=platform,
                        puuid=puuid,
                        verification_method="RSO",
                    )
                )
                record.platform = platform
                record.puuid = puuid
                record.riot_game_name = game_name[:100]
                record.riot_tag_line = tag_line[:20]
                record.status = VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value
                record.error_code = None
                record.next_attempt_at = now
                record.updated_at = now
                await session.flush()
        except IntegrityError:
            await self.fail(session_id, "LINK_CONFLICT")
            return LinkReservationResult.RIOT_ALREADY_LINKED
        return LinkReservationResult.RESERVED

    @staticmethod
    async def _fail_reservation(
        record: VerificationSession,
        result: LinkReservationResult,
        now: datetime,
    ) -> LinkReservationResult:
        record.status = VerificationSessionStatus.FAILED.value
        record.error_code = result.value
        record.updated_at = now
        record.completed_at = now
        return result

    async def fail(self, session_id: int, error_code: str) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None or record.status in TERMINAL_SESSION_STATUSES:
                return
            record.status = VerificationSessionStatus.FAILED.value
            record.error_code = error_code[:64]
            record.updated_at = now
            record.completed_at = now

    async def claim_pending(self, *, limit: int = 5) -> list[VerificationSession]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(minutes=5)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationSession)
                .where(
                    or_(
                        (
                            (
                                VerificationSession.status
                                == VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value
                            )
                            & (VerificationSession.next_attempt_at <= now)
                        ),
                        (
                            (
                                VerificationSession.status
                                == VerificationSessionStatus.APPLYING_DISCORD.value
                            )
                            & (VerificationSession.updated_at <= stale_claim)
                        ),
                    )
                )
                .order_by(VerificationSession.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            records = list(result)
            for record in records:
                record.status = VerificationSessionStatus.APPLYING_DISCORD.value
                record.completion_attempts += 1
                record.updated_at = now
            return records

    async def retry_discord(self, session_id: int, *, error_code: str, delay_seconds: int) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None or record.status != VerificationSessionStatus.APPLYING_DISCORD.value:
                return
            record.status = VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value
            record.error_code = error_code[:64]
            record.next_attempt_at = now + timedelta(seconds=delay_seconds)
            record.updated_at = now

    async def complete_discord(self, session_id: int, *, message_id: int | None) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None or record.status != VerificationSessionStatus.APPLYING_DISCORD.value:
                return False
            if message_id is not None and record.puuid is not None:
                link = await session.get(
                    VerificationLink,
                    (record.guild_id, record.discord_user_id),
                    with_for_update=True,
                )
                if link is not None and link.puuid == record.puuid:
                    link.message_id = message_id
            record.status = VerificationSessionStatus.COMPLETED.value
            record.error_code = None
            record.updated_at = now
            record.completed_at = now
            return True

    async def fail_discord(self, session_id: int, error_code: str) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None or record.status in TERMINAL_SESSION_STATUSES:
                return
            link = await session.get(
                VerificationLink,
                (record.guild_id, record.discord_user_id),
                with_for_update=True,
            )
            if link is not None and link.puuid == record.puuid and link.message_id is None:
                await session.delete(link)
            record.status = VerificationSessionStatus.FAILED.value
            record.error_code = error_code[:64]
            record.updated_at = now
            record.completed_at = now

    async def cancel_for_user(self, guild_id: int, user_id: int) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await session.execute(
                update(VerificationSession)
                .where(
                    VerificationSession.guild_id == guild_id,
                    VerificationSession.discord_user_id == user_id,
                    VerificationSession.status.in_(ACTIVE_SESSION_STATUSES),
                )
                .values(
                    status=VerificationSessionStatus.CANCELLED.value,
                    error_code="USER_REMOVED_LINK",
                    updated_at=now,
                    completed_at=now,
                )
            )

    async def expire_and_purge(self, *, retention_days: int) -> tuple[int, int]:
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=retention_days)
        async with self._sessions.begin() as session:
            expired = await session.execute(
                update(VerificationSession)
                .where(
                    VerificationSession.status.in_(EXPIRABLE_SESSION_STATUSES),
                    VerificationSession.expires_at <= now,
                )
                .values(
                    status=VerificationSessionStatus.EXPIRED.value,
                    error_code="SESSION_EXPIRED",
                    updated_at=now,
                    completed_at=now,
                )
            )
            purged = await session.execute(
                delete(VerificationSession).where(
                    VerificationSession.status.in_(TERMINAL_SESSION_STATUSES),
                    VerificationSession.completed_at < cutoff,
                )
            )
            return (
                cast(CursorResult[Any], expired).rowcount or 0,
                cast(CursorResult[Any], purged).rowcount or 0,
            )
