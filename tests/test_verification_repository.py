from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moon_poro.models import Base, VerificationAccessLog, VerificationLink
from moon_poro.rank_refresh import RankSnapshot
from moon_poro.repositories import (
    RankRefreshRequestStatus,
    VerificationDeletionAccessLog,
    VerificationDeletionPolicy,
    VerificationDeletionProcessStatus,
    VerificationDeletionRequestStatus,
    VerificationLinkIdentity,
    VerificationRepository,
)


@pytest_asyncio.fixture
async def verification_repository() -> AsyncIterator[VerificationRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield VerificationRepository(factory)
    finally:
        await engine.dispose()


async def _delete_verification_link(
    repository: VerificationRepository,
    guild_id: int,
    user_id: int,
) -> VerificationLink | None:
    async with repository._sessions.begin() as session:
        link = await session.get(VerificationLink, (guild_id, user_id), with_for_update=True)
        if link is not None:
            await session.delete(link)
        return link


async def test_audit_message_requires_its_channel(
    verification_repository: VerificationRepository,
) -> None:
    with pytest.raises(IntegrityError):
        await verification_repository.create(
            guild_id=123,
            user_id=100,
            message_id=200,
            platform="EUN1",
            puuid="invalid-audit-message",
        )


async def test_create_caches_optional_riot_id_without_breaking_legacy_callers(
    verification_repository: VerificationRepository,
) -> None:
    cached = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
        audit_channel_id=401,
        platform="EUN1",
        puuid="cached-puuid",
        riot_game_name="Moon Poro",
        riot_tag_line="EUNE",
    )
    legacy = await verification_repository.create(
        guild_id=123,
        user_id=102,
        message_id=202,
        audit_channel_id=401,
        platform="EUW1",
        puuid="legacy-puuid",
    )

    assert (cached.riot_game_name, cached.riot_tag_line) == ("Moon Poro", "EUNE")
    assert (legacy.riot_game_name, legacy.riot_tag_line) == (None, None)


async def test_stale_link_identity_cannot_refresh_or_delete_reverified_account(
    verification_repository: VerificationRepository,
) -> None:
    old = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
        audit_channel_id=401,
        platform="EUN1",
        puuid="same-puuid",
        rank_snapshot=RankSnapshot("GOLD"),
    )
    await _delete_verification_link(verification_repository, 123, 101)
    # Windows' wall clock can advance in ~16 ms ticks. Cross at least one tick so
    # this exercises the created_at generation guard even on SQLite.
    await asyncio.sleep(0.05)
    replacement = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=202,
        audit_channel_id=401,
        platform="EUN1",
        puuid="same-puuid",
        rank_snapshot=RankSnapshot("SILVER"),
    )
    stale_created_at = old.created_at
    if stale_created_at == replacement.created_at:
        # SQLite/Windows can round both inserts to the same coarse clock tick. The
        # production PostgreSQL path preserves the actual generation timestamp.
        stale_created_at -= timedelta(microseconds=1)
    refresh = await verification_repository.request_rank_refresh(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
        expected_puuid=old.puuid,
        expected_platform=old.platform,
        expected_created_at=stale_created_at,
    )
    deletion = await verification_repository.request_verification_deletion_with_identity(
        123,
        101,
        identity=VerificationLinkIdentity(
            puuid=old.puuid,
            platform=old.platform,
            created_at=stale_created_at,
        ),
        policy=VerificationDeletionPolicy.USER,
    )
    current = await verification_repository.get_by_user(123, 101)

    assert refresh.status == RankRefreshRequestStatus.LINK_CHANGED
    assert deletion.status is VerificationDeletionRequestStatus.LINK_CHANGED
    assert current is not None
    assert current.created_at == replacement.created_at
    assert current.rank_user_refresh_requested_at is None
    assert current.deletion_requested_at is None


async def test_matching_identity_requests_deletion_and_blocks_rank_refresh(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
        audit_channel_id=401,
        platform="EUN1",
        puuid="current-puuid",
        rank_snapshot=RankSnapshot("EMERALD"),
    )
    async with verification_repository._sessions.begin() as session:
        stored = await session.get(VerificationLink, (123, 101), with_for_update=True)
        assert stored is not None
        stored.rank_refresh_claimed_at = datetime.now(UTC)

    refresh = await verification_repository.request_rank_refresh(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )
    deletion = await verification_repository.request_verification_deletion_with_identity(
        123,
        101,
        identity=VerificationLinkIdentity.from_link(link),
        policy=VerificationDeletionPolicy.USER,
    )
    blocked_refresh = await verification_repository.request_rank_refresh(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )

    assert refresh.status == RankRefreshRequestStatus.ALREADY_CLAIMED
    assert deletion.status is VerificationDeletionRequestStatus.REQUESTED
    assert deletion.link is not None
    assert deletion.link.deletion_requested_at is not None
    assert blocked_refresh.status == RankRefreshRequestStatus.NOT_LINKED


async def test_rank_refresh_request_returns_snapshot_baseline_from_locked_link(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
        audit_channel_id=401,
        platform="EUN1",
        puuid="current-puuid",
        rank_snapshot=RankSnapshot("EMERALD"),
    )
    async with verification_repository._sessions.begin() as session:
        stored = await session.get(VerificationLink, (123, 101), with_for_update=True)
        assert stored is not None
        stored.rank_refresh_claimed_at = datetime.now(UTC)

    result = await verification_repository.request_rank_refresh(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )

    assert result.status is RankRefreshRequestStatus.ALREADY_CLAIMED
    assert result.baseline_rank_last_checked_at is not None
    assert result.baseline_rank_last_checked_at.replace(tzinfo=UTC) == link.rank_last_checked_at


async def test_null_puuid_identity_and_claim_token_fence_deletion_lifecycle(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=201,
        message_id=None,
        audit_channel_id=None,
        platform="EUN1",
        puuid=None,
    )
    identity = VerificationLinkIdentity.from_link(link)
    wrong_identity = VerificationLinkIdentity(
        puuid="different-puuid",
        platform=link.platform,
        created_at=link.created_at,
    )

    stale_request = await verification_repository.request_verification_deletion_with_identity(
        123,
        201,
        identity=wrong_identity,
        policy=VerificationDeletionPolicy.USER,
    )
    requested = await verification_repository.request_verification_deletion_with_identity(
        123,
        201,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )

    assert stale_request.status is VerificationDeletionRequestStatus.LINK_CHANGED
    assert requested.status is VerificationDeletionRequestStatus.REQUESTED
    assert requested.link is not None
    assert requested.link.puuid is None
    assert not requested.link.deletion_remove_rank_region_roles
    claimed_at = requested.link.deletion_claimed_at
    assert claimed_at is not None
    stale_operation = AsyncMock(return_value=False)
    stale_result = await verification_repository.process_verification_deletion_with_identity(
        123,
        201,
        identity=identity,
        expected_claimed_at=claimed_at - timedelta(microseconds=1),
        base_delay_seconds=1,
        operation=stale_operation,
    )
    retry_operation = AsyncMock(return_value=False)
    retry_result = await verification_repository.process_verification_deletion_with_identity(
        123,
        201,
        identity=identity,
        expected_claimed_at=claimed_at,
        base_delay_seconds=1,
        operation=retry_operation,
    )

    assert stale_result.status is VerificationDeletionProcessStatus.CLAIM_LOST
    stale_operation.assert_not_awaited()
    assert retry_result.status is VerificationDeletionProcessStatus.RETRY_SCHEDULED
    assert retry_result.retry_after_seconds is not None
    retry_operation.assert_awaited_once_with()

    async with verification_repository._sessions.begin() as session:
        stored = await session.get(VerificationLink, (123, 201), with_for_update=True)
        assert stored is not None
        stored.deletion_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    claimed = await verification_repository.claim_due_verification_deletions(
        123,
        limit=1,
        claim_timeout_seconds=300,
    )
    assert [item.discord_user_id for item in claimed] == [201]
    new_claimed_at = claimed[0].deletion_claimed_at
    assert new_claimed_at is not None
    old_claim_operation = AsyncMock(return_value=True)
    old_claim_result = await verification_repository.process_verification_deletion_with_identity(
        123,
        201,
        identity=identity,
        expected_claimed_at=claimed_at,
        base_delay_seconds=1,
        operation=old_claim_operation,
    )
    delete_operation = AsyncMock(return_value=True)
    delete_result = await verification_repository.process_verification_deletion_with_identity(
        123,
        201,
        identity=identity,
        expected_claimed_at=new_claimed_at,
        base_delay_seconds=1,
        operation=delete_operation,
    )

    assert old_claim_result.status is VerificationDeletionProcessStatus.CLAIM_LOST
    old_claim_operation.assert_not_awaited()
    assert delete_result.status is VerificationDeletionProcessStatus.DELETED
    delete_operation.assert_awaited_once_with()
    assert await verification_repository.get_by_user(123, 201) is None


async def test_deletion_policy_is_idempotent_and_conflicts_do_not_change_policy(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=202,
        message_id=301,
        audit_channel_id=401,
        platform="EUW1",
        puuid="user-policy-puuid",
    )
    identity = VerificationLinkIdentity.from_link(link)
    first = await verification_repository.request_verification_deletion_with_identity(
        123,
        202,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    repeated = await verification_repository.request_verification_deletion_with_identity(
        123,
        202,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    conflict = await verification_repository.request_verification_deletion_with_identity(
        123,
        202,
        identity=identity,
        policy=VerificationDeletionPolicy.ADMIN,
        access_log=VerificationDeletionAccessLog(actor_id=999, reason="Administracyjne usunięcie"),
    )

    assert first.status is VerificationDeletionRequestStatus.REQUESTED
    assert repeated.status is VerificationDeletionRequestStatus.ALREADY_REQUESTED
    assert conflict.status is VerificationDeletionRequestStatus.POLICY_CONFLICT
    assert first.link is not None and repeated.link is not None and conflict.link is not None
    assert repeated.link.deletion_claimed_at is not None
    assert first.link.deletion_claimed_at is not None
    assert repeated.link.deletion_claimed_at.replace(
        tzinfo=UTC
    ) == first.link.deletion_claimed_at.replace(tzinfo=UTC)
    assert not conflict.link.deletion_remove_rank_region_roles
    async with verification_repository._sessions() as session:
        logs = list(
            await session.scalars(
                select(VerificationAccessLog).where(VerificationAccessLog.discord_user_id == 202)
            )
        )
    assert logs == []


async def test_admin_deletion_atomically_persists_policy_and_truncated_access_log(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=203,
        message_id=302,
        audit_channel_id=401,
        platform="EUN1",
        puuid="admin-policy-puuid",
    )
    identity = VerificationLinkIdentity.from_link(link)
    access_log = VerificationDeletionAccessLog(actor_id=999, reason=f"  {'x' * 350}  ")

    with pytest.raises(ValueError, match="requires an access log"):
        await verification_repository.request_verification_deletion_with_identity(
            123,
            203,
            identity=identity,
            policy=VerificationDeletionPolicy.ADMIN,
        )
    unchanged = await verification_repository.get_by_user(123, 203)
    assert unchanged is not None and unchanged.deletion_requested_at is None

    requested = await verification_repository.request_verification_deletion_with_identity(
        123,
        203,
        identity=identity,
        policy=VerificationDeletionPolicy.ADMIN,
        access_log=access_log,
    )
    repeated = await verification_repository.request_verification_deletion_with_identity(
        123,
        203,
        identity=identity,
        policy=VerificationDeletionPolicy.ADMIN,
        access_log=access_log,
    )

    assert requested.status is VerificationDeletionRequestStatus.REQUESTED
    assert repeated.status is VerificationDeletionRequestStatus.ALREADY_REQUESTED
    assert requested.link is not None
    assert requested.link.deletion_remove_rank_region_roles
    assert requested.link.audit_channel_id == 401
    async with verification_repository._sessions() as session:
        logs = list(
            await session.scalars(
                select(VerificationAccessLog).where(VerificationAccessLog.discord_user_id == 203)
            )
        )
    assert len(logs) == 1
    assert logs[0].actor_id == 999
    assert logs[0].puuid == "admin-policy-puuid"
    assert logs[0].reason == "x" * 300


async def test_guarded_deletion_deletes_only_after_successful_operation(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=204,
        message_id=303,
        audit_channel_id=401,
        platform="EUN1",
        puuid=None,
    )
    identity = VerificationLinkIdentity.from_link(link)
    request = await verification_repository.request_verification_deletion_with_identity(
        123,
        204,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    assert request.link is not None
    claimed_at = request.link.deletion_claimed_at
    assert claimed_at is not None
    operation = AsyncMock(return_value=True)

    result = await verification_repository.process_verification_deletion_with_identity(
        123,
        204,
        identity=identity,
        expected_claimed_at=claimed_at,
        base_delay_seconds=10,
        operation=operation,
    )

    assert result.status is VerificationDeletionProcessStatus.DELETED
    assert result.retry_after_seconds is None
    operation.assert_awaited_once_with()
    assert await verification_repository.get_by_user(123, 204) is None


async def test_guarded_deletion_schedules_retry_after_failed_operation(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=205,
        message_id=304,
        audit_channel_id=401,
        platform="EUW1",
        puuid="retry-puuid",
    )
    identity = VerificationLinkIdentity.from_link(link)
    request = await verification_repository.request_verification_deletion_with_identity(
        123,
        205,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    assert request.link is not None
    claimed_at = request.link.deletion_claimed_at
    assert claimed_at is not None

    result = await verification_repository.process_verification_deletion_with_identity(
        123,
        205,
        identity=identity,
        expected_claimed_at=claimed_at,
        base_delay_seconds=10,
        operation=AsyncMock(return_value=False),
    )

    assert result.status is VerificationDeletionProcessStatus.RETRY_SCHEDULED
    assert result.retry_after_seconds is not None
    stored = await verification_repository.get_by_user(123, 205)
    assert stored is not None
    assert stored.deletion_requested_at is not None
    assert stored.deletion_claimed_at is None
    assert stored.deletion_failures == 1
    assert stored.deletion_next_attempt_at is not None


async def test_guarded_deletion_rejects_stale_identity_and_claim_before_operation(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=206,
        message_id=305,
        audit_channel_id=401,
        platform="EUN1",
        puuid=None,
    )
    identity = VerificationLinkIdentity.from_link(link)
    request = await verification_repository.request_verification_deletion_with_identity(
        123,
        206,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    assert request.link is not None
    claimed_at = request.link.deletion_claimed_at
    assert claimed_at is not None
    operation = AsyncMock(return_value=True)

    stale_identity = await verification_repository.process_verification_deletion_with_identity(
        123,
        206,
        identity=VerificationLinkIdentity(
            puuid="unexpected-puuid",
            platform=identity.platform,
            created_at=identity.created_at,
        ),
        expected_claimed_at=claimed_at,
        base_delay_seconds=10,
        operation=operation,
    )
    stale_claim = await verification_repository.process_verification_deletion_with_identity(
        123,
        206,
        identity=identity,
        expected_claimed_at=claimed_at - timedelta(microseconds=1),
        base_delay_seconds=10,
        operation=operation,
    )

    assert stale_identity.status is VerificationDeletionProcessStatus.LINK_CHANGED
    assert stale_claim.status is VerificationDeletionProcessStatus.CLAIM_LOST
    operation.assert_not_awaited()


async def test_guarded_role_update_requires_current_active_link(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=207,
        message_id=306,
        audit_channel_id=401,
        platform="EUN1",
        puuid=None,
    )
    identity = VerificationLinkIdentity.from_link(link)
    active_operation = AsyncMock()

    applied = await verification_repository.run_verification_role_update_with_identity(
        123,
        207,
        identity=identity,
        operation=active_operation,
    )

    assert applied
    active_operation.assert_awaited_once_with()

    stale_operation = AsyncMock()
    stale = await verification_repository.run_verification_role_update_with_identity(
        123,
        207,
        identity=VerificationLinkIdentity(
            puuid="stale-puuid",
            platform=identity.platform,
            created_at=identity.created_at,
        ),
        operation=stale_operation,
    )

    assert not stale
    stale_operation.assert_not_awaited()

    request = await verification_repository.request_verification_deletion_with_identity(
        123,
        207,
        identity=identity,
        policy=VerificationDeletionPolicy.USER,
    )
    assert request.status is VerificationDeletionRequestStatus.REQUESTED
    deleting_operation = AsyncMock()

    deleting = await verification_repository.run_verification_role_update_with_identity(
        123,
        207,
        identity=identity,
        operation=deleting_operation,
    )

    assert not deleting
    deleting_operation.assert_not_awaited()


async def test_guarded_pending_deletion_role_cleanup_rejects_stale_event(
    verification_repository: VerificationRepository,
) -> None:
    old_link = await verification_repository.create(
        guild_id=123,
        user_id=208,
        message_id=307,
        audit_channel_id=407,
        platform="EUN1",
        puuid="old-puuid",
    )
    old_identity = VerificationLinkIdentity.from_link(old_link)
    request = await verification_repository.request_verification_deletion_with_identity(
        123,
        208,
        identity=old_identity,
        policy=VerificationDeletionPolicy.USER,
    )
    assert request.link is not None
    requested_at = request.link.deletion_requested_at
    assert requested_at is not None

    wrong_policy_operation = AsyncMock()
    wrong_policy = (
        await verification_repository.run_verification_deletion_role_cleanup_with_identity(
            123,
            208,
            identity=old_identity,
            expected_requested_at=requested_at,
            expected_remove_rank_region_roles=True,
            operation=wrong_policy_operation,
        )
    )
    assert not wrong_policy
    wrong_policy_operation.assert_not_awaited()

    current_operation = AsyncMock()
    current = await verification_repository.run_verification_deletion_role_cleanup_with_identity(
        123,
        208,
        identity=old_identity,
        expected_requested_at=requested_at,
        expected_remove_rank_region_roles=False,
        operation=current_operation,
    )
    assert current
    current_operation.assert_awaited_once_with()

    await _delete_verification_link(verification_repository, 123, 208)
    await asyncio.sleep(0.05)
    replacement = await verification_repository.create(
        guild_id=123,
        user_id=208,
        message_id=308,
        audit_channel_id=408,
        platform="EUW1",
        puuid="new-puuid",
    )
    stale_operation = AsyncMock()

    stale = await verification_repository.run_verification_deletion_role_cleanup_with_identity(
        123,
        208,
        identity=old_identity,
        expected_requested_at=requested_at,
        expected_remove_rank_region_roles=False,
        operation=stale_operation,
    )

    assert not stale
    stale_operation.assert_not_awaited()
    assert await verification_repository.get_by_user(123, 208) is not None
    assert replacement.puuid == "new-puuid"
