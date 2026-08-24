from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moon_poro.models import (
    Base,
    VerificationLink,
    VerificationSession,
    VerificationSessionStatus,
)
from moon_poro.verification_sessions import (
    CreatedVerificationSession,
    LinkReservationResult,
    SessionAlreadyUsed,
    SessionExpired,
    VerificationSessionRepository,
    hash_secret,
)


@pytest_asyncio.fixture
async def session_repository() -> AsyncIterator[VerificationSessionRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield VerificationSessionRepository(factory)
    finally:
        await engine.dispose()


async def authorize_session(
    repository: VerificationSessionRepository,
    *,
    guild_id: int,
    user_id: int,
    state: str,
) -> tuple[CreatedVerificationSession, VerificationSession]:
    created = await repository.create(guild_id=guild_id, user_id=user_id, ttl_seconds=600)
    started = await repository.begin_oauth(token=created.token, state=state)
    assert started.oauth_state_hash == hash_secret(state)
    return created, await repository.claim_callback(state)


async def test_complete_rso_lifecycle(
    session_repository: VerificationSessionRepository,
) -> None:
    created, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="first-state",
    )

    result = await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="player-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )
    pending = await session_repository.claim_pending()
    completed = await session_repository.complete_discord(
        pending[0].id,
        message_id=987,
        channel_id=654,
    )
    stored = await session_repository.get_by_start_token(created.token)
    async with session_repository._sessions() as session:
        link = await session.get(VerificationLink, (123, 456))

    assert result == LinkReservationResult.RESERVED
    assert completed is True
    assert stored is not None
    assert stored.status == VerificationSessionStatus.COMPLETED.value
    assert stored.riot_game_name == "Moon"
    assert stored.error_code is None
    assert link is not None
    assert link.riot_game_name == "Moon"
    assert link.riot_tag_line == "EUNE"
    assert link.message_id == 987
    assert link.audit_channel_id == 654


async def test_new_session_supersedes_previous_link(
    session_repository: VerificationSessionRepository,
) -> None:
    first = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    second = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)

    first_record = await session_repository.get_by_start_token(first.token)
    second_record = await session_repository.get_by_start_token(second.token)

    assert first_record is not None
    assert first_record.status == VerificationSessionStatus.CANCELLED.value
    assert first_record.error_code == "SUPERSEDED"
    assert second_record is not None
    assert second_record.status == VerificationSessionStatus.CREATED.value


async def test_cancel_for_user_only_cancels_active_sessions_created_by_cutoff(
    session_repository: VerificationSessionRepository,
) -> None:
    older = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    older_record = await session_repository.get_by_start_token(older.token)
    assert older_record is not None
    cutoff = older_record.created_at
    newer_token = "newer-session-token"
    newer_created_at = cutoff + timedelta(seconds=1)
    async with session_repository._sessions.begin() as session:
        session.add(
            VerificationSession(
                guild_id=123,
                discord_user_id=456,
                start_token_hash=hash_secret(newer_token),
                status=VerificationSessionStatus.CREATED.value,
                created_at=newer_created_at,
                expires_at=newer_created_at + timedelta(minutes=10),
                updated_at=newer_created_at,
            )
        )

    await session_repository.cancel_for_user(
        123,
        456,
        created_at_or_before=cutoff,
    )

    cancelled = await session_repository.get_by_start_token(older.token)
    still_active = await session_repository.get_by_start_token(newer_token)
    assert cancelled is not None
    assert cancelled.status == VerificationSessionStatus.CANCELLED.value
    assert cancelled.error_code == "USER_REMOVED_LINK"
    assert cancelled.completed_at is not None
    assert still_active is not None
    assert still_active.status == VerificationSessionStatus.CREATED.value
    assert still_active.error_code is None
    assert still_active.completed_at is None


async def test_oauth_state_is_one_time(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    await session_repository.begin_oauth(token=created.token, state="one-time-state")
    await session_repository.claim_callback("one-time-state")

    with pytest.raises(SessionAlreadyUsed):
        await session_repository.claim_callback("one-time-state")


async def test_expired_session_cannot_start(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=-1)

    with pytest.raises(SessionExpired):
        await session_repository.begin_oauth(token=created.token, state="expired-state")

    expired, purged = await session_repository.expire_and_purge(retention_days=7)
    assert (expired, purged) == (1, 0)


async def test_same_riot_account_cannot_link_to_two_discord_users(
    session_repository: VerificationSessionRepository,
) -> None:
    _, first = await authorize_session(
        session_repository, guild_id=123, user_id=1, state="state-one"
    )
    assert (
        await session_repository.reserve_link(
            session_id=first.id,
            platform="EUN1",
            puuid="shared-puuid",
            game_name="First",
            tag_line="EUNE",
        )
        == LinkReservationResult.RESERVED
    )
    second_created, second = await authorize_session(
        session_repository, guild_id=123, user_id=2, state="state-two"
    )

    result = await session_repository.reserve_link(
        session_id=second.id,
        platform="EUN1",
        puuid="shared-puuid",
        game_name="Second",
        tag_line="EUNE",
    )
    second_record = await session_repository.get_by_start_token(second_created.token)

    assert result == LinkReservationResult.RIOT_ALREADY_LINKED
    assert second_record is not None
    assert second_record.status == VerificationSessionStatus.FAILED.value


async def test_existing_discord_link_rejects_rso_reservation(
    session_repository: VerificationSessionRepository,
) -> None:
    async with session_repository._sessions.begin() as session:
        session.add(
            VerificationLink(
                guild_id=123,
                discord_user_id=456,
                message_id=5,
                audit_channel_id=6,
                platform="EUN1",
                puuid="old-puuid",
                verification_method="PROFILE_ICON",
            )
        )
    _, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="existing-discord",
    )

    result = await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUW1",
        puuid="new-puuid",
        game_name="New",
        tag_line="EUW",
    )

    assert result == LinkReservationResult.DISCORD_ALREADY_LINKED


async def test_cancellation_wins_race_with_discord_completion(
    session_repository: VerificationSessionRepository,
) -> None:
    _, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="cancel-state",
    )
    await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="cancel-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )
    pending = await session_repository.claim_pending()

    await session_repository.cancel_for_user(
        123,
        456,
        created_at_or_before=pending[0].created_at,
    )
    completed = await session_repository.complete_discord(
        pending[0].id,
        message_id=987,
        channel_id=654,
    )

    assert completed is False
    cleanups = await session_repository.claim_audit_cleanups(
        limit=1,
        claim_timeout_seconds=300,
    )
    assert [(item.channel_id, item.message_id) for item in cleanups] == [(654, 987)]


async def test_orphan_audit_cleanup_survives_retry_and_repository_restart(
    session_repository: VerificationSessionRepository,
) -> None:
    _, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="orphan-audit-state",
    )
    await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="orphan-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )
    pending = await session_repository.claim_pending()
    await session_repository.cancel_for_user(
        123,
        456,
        created_at_or_before=pending[0].created_at,
    )
    assert not await session_repository.complete_discord(
        pending[0].id,
        message_id=999,
        channel_id=654,
    )
    claimed = await session_repository.claim_audit_cleanups(
        limit=1,
        claim_timeout_seconds=300,
    )
    assert len(claimed) == 1
    cleanup = claimed[0]
    assert (
        await session_repository.retry_audit_cleanup(
            cleanup.id,
            message_id=cleanup.message_id,
            base_delay_seconds=0,
        )
        == 0
    )

    restarted = VerificationSessionRepository(session_repository._sessions)
    reclaimed = await restarted.claim_audit_cleanups(limit=1, claim_timeout_seconds=300)
    assert [item.id for item in reclaimed] == [cleanup.id]
    assert await restarted.acknowledge_audit_cleanup(
        cleanup.id,
        message_id=cleanup.message_id,
    )
    assert await restarted.claim_audit_cleanups(limit=1, claim_timeout_seconds=300) == []


async def test_retry_returns_claim_to_pending_queue(
    session_repository: VerificationSessionRepository,
) -> None:
    created, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="retry-state",
    )
    await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="retry-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )
    pending = await session_repository.claim_pending()

    await session_repository.retry_discord(
        pending[0].id,
        error_code="RIOT_API_UNAVAILABLE",
        delay_seconds=0,
    )
    reclaimed = await session_repository.claim_pending()
    stored = await session_repository.get_by_start_token(created.token)

    assert [record.id for record in reclaimed] == [pending[0].id]
    assert stored is not None
    assert stored.completion_attempts == 2


async def test_fail_discord_removes_unfinished_reserved_link(
    session_repository: VerificationSessionRepository,
) -> None:
    created, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="failure-state",
    )
    await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="failed-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )

    await session_repository.fail_discord(callback.id, "MEMBER_LEFT_GUILD")
    stored = await session_repository.get_by_start_token(created.token)

    assert stored is not None
    assert stored.status == VerificationSessionStatus.FAILED.value
    assert stored.error_code == "MEMBER_LEFT_GUILD"


async def test_fail_discord_preserves_link_with_pending_delete_tombstone(
    session_repository: VerificationSessionRepository,
) -> None:
    _, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="delete-race-state",
    )
    await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="delete-race-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )
    async with session_repository._sessions.begin() as session:
        link = await session.get(VerificationLink, (123, 456), with_for_update=True)
        assert link is not None
        link.deletion_requested_at = datetime.now(UTC)

    await session_repository.fail_discord(callback.id, "VERIFICATION_LINK_CHANGED")

    async with session_repository._sessions() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert link is not None
        assert link.deletion_requested_at is not None
