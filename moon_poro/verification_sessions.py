from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from moon_poro.models import (
    VerificationAuditCleanup,
    VerificationLink,
    VerificationSession,
    VerificationSessionStatus,
)
from moon_poro.outbox import mark_outbox_claimed, reschedule_outbox_record

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


class SessionBrowserMismatch(SessionError):
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

    @staticmethod
    async def _lock_user(session: AsyncSession, guild_id: int, user_id: int) -> None:
        # Row locks cannot serialize two operations before a link exists.
        # Transaction-scoped locks also work across the web and bot processes.
        if session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(f"moon-poro:rso:{guild_id}:{user_id}".encode()).digest()[:8],
                signed=True,
            )
            await session.execute(select(func.pg_advisory_xact_lock(key)))

    async def create(
        self, *, guild_id: int, user_id: int, ttl_seconds: int, method: str = "RSO"
    ) -> CreatedVerificationSession:
        if method not in ("RSO", "PROFILE_ICON"):
            raise ValueError("Unsupported verification method")
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        record = VerificationSession(
            guild_id=guild_id,
            discord_user_id=user_id,
            verification_method=method,
            start_token_hash=hash_secret(token),
            status=VerificationSessionStatus.CREATED.value,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            updated_at=now,
        )
        async with self._sessions.begin() as session:
            await self._lock_user(session, guild_id, user_id)
            existing_link = await session.get(VerificationLink, (guild_id, user_id))
            finishing = await session.scalar(
                select(VerificationSession.id)
                .where(
                    VerificationSession.guild_id == guild_id,
                    VerificationSession.discord_user_id == user_id,
                    VerificationSession.status.in_(
                        (
                            VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value,
                            VerificationSessionStatus.APPLYING_DISCORD.value,
                        )
                    ),
                )
                .limit(1)
            )
            if existing_link is not None or finishing is not None:
                raise SessionAlreadyUsed
            await session.execute(
                update(VerificationSession)
                .where(
                    VerificationSession.guild_id == guild_id,
                    VerificationSession.discord_user_id == user_id,
                    VerificationSession.status.in_(EXPIRABLE_SESSION_STATUSES),
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

    async def begin_oauth(
        self, *, token: str, state: str, browser_token: str
    ) -> VerificationSession:
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
            if (
                record.verification_method != "RSO"
                or record.status != VerificationSessionStatus.CREATED.value
            ):
                raise SessionAlreadyUsed
            record.oauth_state_hash = hash_secret(state)
            record.browser_binding_hash = hash_secret(browser_token)
            record.status = VerificationSessionStatus.AWAITING_RIOT.value
            record.updated_at = now
            return record

    async def claim_callback(self, state: str, *, browser_token: str) -> VerificationSession:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.scalar(
                select(VerificationSession)
                .where(VerificationSession.oauth_state_hash == hash_secret(state))
                .with_for_update()
            )
            if record is None:
                raise SessionNotFound
            if (
                not browser_token
                or not record.browser_binding_hash
                or not secrets.compare_digest(
                    record.browser_binding_hash, hash_secret(browser_token)
                )
            ):
                # A foreign browser must not consume even a valid OAuth state.
                raise SessionBrowserMismatch
            if as_utc(record.expires_at) <= now:
                record.status = VerificationSessionStatus.EXPIRED.value
                record.error_code = "SESSION_EXPIRED"
                record.updated_at = now
                record.completed_at = now
                raise SessionExpired
            if (
                record.verification_method != "RSO"
                or record.status != VerificationSessionStatus.AWAITING_RIOT.value
            ):
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
                identity = (
                    await session.execute(
                        select(
                            VerificationSession.guild_id, VerificationSession.discord_user_id
                        ).where(VerificationSession.id == session_id)
                    )
                ).one_or_none()
                if identity is None:
                    raise SessionNotFound
                # Same lock order as create(): user first, then session/link.
                await self._lock_user(session, identity.guild_id, identity.discord_user_id)
                record = await session.scalar(
                    select(VerificationSession)
                    .where(VerificationSession.id == session_id)
                    .with_for_update()
                )
                if record is None:
                    raise SessionNotFound
                expected_status = (
                    VerificationSessionStatus.CREATED.value
                    if record.verification_method == "PROFILE_ICON"
                    else VerificationSessionStatus.PROCESSING_RIOT.value
                )
                if record.status != expected_status:
                    raise SessionAlreadyUsed
                now = datetime.now(UTC)
                if as_utc(record.expires_at) <= now:
                    raise SessionExpired

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
                        riot_game_name=game_name[:100],
                        riot_tag_line=tag_line[:20],
                        verification_method=record.verification_method,
                        rank_next_refresh_at=now + timedelta(hours=24),
                        created_at=now,
                    )
                )
                record.platform = platform
                record.puuid = puuid
                record.riot_game_name = game_name[:100]
                record.riot_tag_line = tag_line[:20]
                record.verification_link_created_at = now
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

    async def claim_pending(
        self, *, limit: int = 5, method: str | None = "RSO"
    ) -> list[VerificationSession]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(minutes=5)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationSession)
                .where(
                    (VerificationSession.verification_method == method) if method else true(),
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
                    ),
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
            if record is None:
                return
            if record.status == VerificationSessionStatus.APPLYING_DISCORD.value:
                record.status = VerificationSessionStatus.VERIFIED_PENDING_DISCORD.value
                record.error_code = error_code[:64]
            elif (
                record.status not in TERMINAL_SESSION_STATUSES
                or record.audit_pending_channel_id is None
            ):
                return
            record.next_attempt_at = now + timedelta(seconds=delay_seconds)
            record.updated_at = now

    async def claim_audit_reconciliations(
        self, *, limit: int = 5, method: str | None = None
    ) -> list[VerificationSession]:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            records = list(
                await session.scalars(
                    select(VerificationSession)
                    .where(
                        (VerificationSession.verification_method == method) if method else true(),
                        VerificationSession.status.in_(TERMINAL_SESSION_STATUSES),
                        VerificationSession.audit_pending_channel_id.is_not(None),
                        VerificationSession.next_attempt_at <= now,
                    )
                    .order_by(VerificationSession.next_attempt_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for record in records:
                record.next_attempt_at = now + timedelta(minutes=5)
                record.updated_at = now
            return records

    @staticmethod
    def _link_matches(record: VerificationSession, link: VerificationLink | None) -> bool:
        return bool(
            record.status == VerificationSessionStatus.APPLYING_DISCORD.value
            and link is not None
            and link.deletion_requested_at is None
            and record.puuid is not None
            and link.puuid == record.puuid
            and link.platform == record.platform
            and record.verification_link_created_at is not None
            and as_utc(link.created_at) == as_utc(record.verification_link_created_at)
        )

    async def publish_audit(
        self,
        session_id: int,
        *,
        channel_id: int,
        operation: Callable[[VerificationSession, bool], Awaitable[list[int]]],
    ) -> bool:
        # Commit the destination BEFORE any Discord side effect. A crash after
        # send can then be reconciled from channel history even after cancellation.
        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None:
                return False
            if record.audit_pending_channel_id is None:
                if record.status != VerificationSessionStatus.APPLYING_DISCORD.value:
                    return record.status == VerificationSessionStatus.COMPLETED.value
                record.audit_pending_channel_id = channel_id

        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None:
                return False
            if record.audit_pending_channel_id is None:
                return record.status == VerificationSessionStatus.COMPLETED.value
            link = await session.get(
                VerificationLink, (record.guild_id, record.discord_user_id), with_for_update=True
            )
            active = self._link_matches(record, link)
            # Keep locks during Discord reconciliation: another worker or deletion
            # cannot race a send with cancellation and lose its message identity.
            message_ids = await operation(record, active)
            if active and not message_ids:
                raise RuntimeError("Audit publication returned no message")
            now = datetime.now(UTC)
            for message_id in message_ids[1:] if active else message_ids:
                await self._enqueue_audit_cleanup(
                    session,
                    record=record,
                    channel_id=record.audit_pending_channel_id,
                    message_id=message_id,
                    now=now,
                )
            if active:
                link = cast(VerificationLink, link)
                link.message_id = message_ids[0]
                link.audit_channel_id = record.audit_pending_channel_id
                record.status = VerificationSessionStatus.COMPLETED.value
                record.error_code = None
                record.completed_at = now
            elif record.status not in TERMINAL_SESSION_STATUSES:
                record.status = VerificationSessionStatus.CANCELLED.value
                record.error_code = "VERIFICATION_LINK_CHANGED"
                record.completed_at = now
            record.audit_pending_channel_id = None
            record.updated_at = now
            return active

    async def complete_discord(
        self,
        session_id: int,
        *,
        message_id: int,
        channel_id: int,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(VerificationSession, session_id, with_for_update=True)
            if record is None:
                return False
            link = await session.get(
                VerificationLink,
                (record.guild_id, record.discord_user_id),
                with_for_update=True,
            )
            link_matches = self._link_matches(record, link)
            if not link_matches:
                if message_id is not None and channel_id is not None:
                    await self._enqueue_audit_cleanup(
                        session,
                        record=record,
                        channel_id=channel_id,
                        message_id=message_id,
                        now=now,
                    )
                return False
            link = cast(VerificationLink, link)
            if message_id is not None:
                link.message_id = message_id
                link.audit_channel_id = channel_id
            record.status = VerificationSessionStatus.COMPLETED.value
            record.audit_pending_channel_id = None
            record.error_code = None
            record.updated_at = now
            record.completed_at = now
            return True

    @staticmethod
    async def _enqueue_audit_cleanup(
        session: AsyncSession,
        *,
        record: VerificationSession,
        channel_id: int,
        message_id: int,
        now: datetime,
    ) -> None:
        existing = await session.scalar(
            select(VerificationAuditCleanup).where(
                VerificationAuditCleanup.channel_id == channel_id,
                VerificationAuditCleanup.message_id == message_id,
            )
        )
        if existing is None:
            session.add(
                VerificationAuditCleanup(
                    verification_session_id=record.id,
                    guild_id=record.guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    next_attempt_at=now,
                    created_at=now,
                )
            )

    async def claim_audit_cleanups(
        self,
        *,
        limit: int,
        claim_timeout_seconds: int,
    ) -> list[VerificationAuditCleanup]:
        now = datetime.now(UTC)
        stale_claim = now - timedelta(seconds=claim_timeout_seconds)
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(VerificationAuditCleanup)
                .where(
                    VerificationAuditCleanup.next_attempt_at <= now,
                    (
                        VerificationAuditCleanup.claimed_at.is_(None)
                        | (VerificationAuditCleanup.claimed_at <= stale_claim)
                    ),
                )
                .order_by(VerificationAuditCleanup.next_attempt_at, VerificationAuditCleanup.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            return mark_outbox_claimed(result, now)

    async def acknowledge_audit_cleanup(self, cleanup_id: int, *, message_id: int) -> bool:
        async with self._sessions.begin() as session:
            record = await session.get(VerificationAuditCleanup, cleanup_id, with_for_update=True)
            if record is None:
                return True
            if record.message_id != message_id:
                return False
            await session.delete(record)
            return True

    async def retry_audit_cleanup(
        self,
        cleanup_id: int,
        *,
        message_id: int,
        base_delay_seconds: int,
        max_delay_seconds: int = 21_600,
    ) -> int | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            record = await session.get(VerificationAuditCleanup, cleanup_id, with_for_update=True)
            if record is None or record.message_id != message_id:
                return None
            failures = min(int(record.failures) + 1, 16)
            delay: int = min(
                base_delay_seconds * (2 ** (failures - 1)),
                max_delay_seconds,
            )
            reschedule_outbox_record(
                record,
                now=now,
                failures=failures,
                delay_seconds=delay,
            )
            return delay

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
            if (
                link is not None
                and link.deletion_requested_at is None
                and link.puuid == record.puuid
                and link.platform == record.platform
                and record.verification_link_created_at is not None
                and link.created_at == record.verification_link_created_at
                and link.message_id is None
            ):
                await session.delete(link)
            record.status = VerificationSessionStatus.FAILED.value
            record.error_code = error_code[:64]
            record.updated_at = now
            record.completed_at = now

    async def cancel_for_user(
        self,
        guild_id: int,
        user_id: int,
        *,
        created_at_or_before: datetime,
    ) -> None:
        now = datetime.now(UTC)
        cutoff = as_utc(created_at_or_before)
        async with self._sessions.begin() as session:
            await session.execute(
                update(VerificationSession)
                .where(
                    VerificationSession.guild_id == guild_id,
                    VerificationSession.discord_user_id == user_id,
                    VerificationSession.status.in_(ACTIVE_SESSION_STATUSES),
                    VerificationSession.created_at <= cutoff,
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
                    VerificationSession.audit_pending_channel_id.is_(None),
                    VerificationSession.completed_at < cutoff,
                )
            )
            return (
                cast(CursorResult[Any], expired).rowcount or 0,
                cast(CursorResult[Any], purged).rowcount or 0,
            )
