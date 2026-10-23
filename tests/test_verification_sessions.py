from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

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
    SessionBrowserMismatch,
    SessionExpired,
    SessionNotFound,
    VerificationSessionRepository,
    as_utc,
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
    started = await repository.begin_oauth(
        token=created.token, state=state, browser_token="test-browser"
    )
    assert started.oauth_state_hash == hash_secret(state)
    return created, await repository.claim_callback(state, browser_token="test-browser")


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


async def test_legacy_session_cannot_enter_oauth(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(
        guild_id=123, user_id=456, ttl_seconds=120, method="PROFILE_ICON"
    )
    with pytest.raises(SessionAlreadyUsed):
        await session_repository.begin_oauth(
            token=created.token, state="state", browser_token="browser"
        )
    record = await session_repository.get_by_start_token(created.token)
    assert record is not None
    # Even an accidentally stored OAuth state cannot authorize an icon session.
    async with session_repository._sessions.begin() as session:
        stored = await session.get(VerificationSession, record.id)
        assert stored is not None
        stored.oauth_state_hash = hash_secret("state")
        stored.browser_binding_hash = hash_secret("browser")
        stored.status = "AWAITING_RIOT"
    with pytest.raises(SessionAlreadyUsed):
        await session_repository.claim_callback("state", browser_token="browser")


async def test_legacy_latest_session_and_hard_ttl_survive_repository_restart(
    session_repository: VerificationSessionRepository,
) -> None:
    old = await session_repository.create(
        guild_id=123, user_id=456, ttl_seconds=120, method="PROFILE_ICON"
    )
    latest = await session_repository.create(
        guild_id=123, user_id=456, ttl_seconds=120, method="PROFILE_ICON"
    )
    restarted = VerificationSessionRepository(session_repository._sessions)
    old_record = await restarted.get_by_start_token(old.token)
    latest_record = await restarted.get_by_start_token(latest.token)
    assert old_record is not None and latest_record is not None
    with pytest.raises(SessionAlreadyUsed):
        await restarted.reserve_link(
            session_id=old_record.id,
            platform="EUN1",
            puuid="puuid",
            game_name="Moon",
            tag_line="EUNE",
        )
    async with restarted._sessions.begin() as session:
        stored = await session.get(VerificationSession, latest_record.id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(SessionExpired):
        await restarted.reserve_link(
            session_id=latest_record.id,
            platform="EUN1",
            puuid="puuid",
            game_name="Moon",
            tag_line="EUNE",
        )
    assert await restarted.claim_pending(method="PROFILE_ICON") == []


async def test_legacy_pending_work_is_separate_from_rso_after_restart(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(
        guild_id=123, user_id=456, ttl_seconds=120, method="PROFILE_ICON"
    )
    record = await session_repository.get_by_start_token(created.token)
    assert record is not None
    assert (
        await session_repository.reserve_link(
            session_id=record.id, platform="EUN1", puuid="puuid", game_name="Moon", tag_line="EUNE"
        )
        == LinkReservationResult.RESERVED
    )
    restarted = VerificationSessionRepository(session_repository._sessions)
    assert await restarted.claim_pending() == []
    claimed = await restarted.claim_pending(method="PROFILE_ICON")
    assert [item.id for item in claimed] == [record.id]
    async with restarted._sessions() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert link is not None and link.verification_method == "PROFILE_ICON"


async def test_explicit_all_methods_claim_completes_legacy_after_switch_to_rso(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(
        guild_id=123, user_id=456, ttl_seconds=120, method="PROFILE_ICON"
    )
    record = await session_repository.get_by_start_token(created.token)
    assert record is not None
    # An issued icon challenge alone is never work for either completion worker.
    assert await session_repository.claim_pending(method=None) == []
    await session_repository.reserve_link(
        session_id=record.id, platform="EUN1", puuid="puuid", game_name="Moon", tag_line="EUNE"
    )
    claimed = await session_repository.claim_pending(method=None)
    assert [item.id for item in claimed] == [record.id]


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
    await session_repository.begin_oauth(
        token=created.token, state="one-time-state", browser_token="test-browser"
    )
    await session_repository.claim_callback("one-time-state", browser_token="test-browser")

    with pytest.raises(SessionAlreadyUsed):
        await session_repository.claim_callback("one-time-state", browser_token="test-browser")


async def test_expired_session_cannot_start(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=-1)

    with pytest.raises(SessionExpired):
        await session_repository.begin_oauth(
            token=created.token, state="expired-state", browser_token="test-browser"
        )

    expired, purged = await session_repository.expire_and_purge(retention_days=7)
    assert (expired, purged) == (0, 0)
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None
    assert stored.status == VerificationSessionStatus.EXPIRED.value
    assert stored.error_code == "SESSION_EXPIRED"
    assert stored.completed_at is not None


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
    _, callback = await authorize_session(
        session_repository,
        guild_id=123,
        user_id=456,
        state="existing-discord",
    )
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
    result = await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUW1",
        puuid="new-puuid",
        game_name="New",
        tag_line="EUW",
    )

    assert result == LinkReservationResult.DISCORD_ALREADY_LINKED


@pytest.mark.parametrize("browser_token", ["", "another-browser"])
async def test_foreign_browser_does_not_consume_state(
    session_repository: VerificationSessionRepository, browser_token: str
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    await session_repository.begin_oauth(
        token=created.token, state="bound-state", browser_token="correct-browser"
    )
    with pytest.raises(SessionBrowserMismatch):
        await session_repository.claim_callback("bound-state", browser_token=browser_token)
    record = await session_repository.get_by_start_token(created.token)
    assert record is not None and record.status == "AWAITING_RIOT"
    assert record.browser_binding_hash == hash_secret("correct-browser")
    await session_repository.claim_callback("bound-state", browser_token="correct-browser")


@pytest.mark.parametrize("claim_discord", [False, True])
async def test_new_start_cannot_cancel_reserved_completion(
    session_repository: VerificationSessionRepository, claim_discord: bool
) -> None:
    created, callback = await authorize_session(
        session_repository, guild_id=123, user_id=456, state="reserved-state"
    )
    await session_repository.reserve_link(
        session_id=callback.id,
        platform="EUN1",
        puuid="reserved-puuid",
        game_name="Moon",
        tag_line="EUNE",
    )
    if claim_discord:
        await session_repository.claim_pending()
    with pytest.raises(SessionAlreadyUsed):
        await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    record = await session_repository.get_by_start_token(created.token)
    expected = "APPLYING_DISCORD" if claim_discord else "VERIFIED_PENDING_DISCORD"
    assert record is not None and record.status == expected


async def test_unbound_session_from_before_migration_fails_closed(
    session_repository: VerificationSessionRepository,
) -> None:
    await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    async with session_repository._sessions.begin() as session:
        record = await session.get(VerificationSession, 1)
        assert record is not None
        record.oauth_state_hash = hash_secret("old-state")
        record.status = "AWAITING_RIOT"
    with pytest.raises(SessionBrowserMismatch):
        await session_repository.claim_callback("old-state", browser_token="any-browser")


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


async def reserve_completion(
    repository: VerificationSessionRepository,
) -> tuple[CreatedVerificationSession, VerificationSession]:
    created, record = await authorize_session(
        repository, guild_id=123, user_id=456, state="completion-state"
    )
    await repository.reserve_link(
        session_id=record.id, platform="EUN1", puuid="puuid", game_name="Moon", tag_line="EUNE"
    )
    claimed = await repository.claim_pending()
    return created, claimed[0]


async def test_discord_snapshots_are_optional_bounded_and_durable(
    session_repository: VerificationSessionRepository,
) -> None:
    legacy = await session_repository.create(guild_id=123, user_id=1, ttl_seconds=600)
    named = await session_repository.create(
        guild_id=123,
        user_id=2,
        ttl_seconds=600,
        discord_username="u" * 110,
        discord_guild_name="g" * 110,
    )
    restarted = VerificationSessionRepository(session_repository._sessions)
    old = await restarted.get_by_start_token(legacy.token)
    current = await restarted.get_by_start_token(named.token)
    assert old is not None and old.discord_username is None and old.discord_guild_name is None
    assert current is not None
    assert current.discord_username == "u" * 100
    assert current.discord_guild_name == "g" * 100


async def test_callback_expiry_is_persisted_only_for_bound_browser(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    record = await session_repository.begin_oauth(
        token=created.token, state="expired-callback", browser_token="correct"
    )
    async with session_repository._sessions.begin() as session:
        stored = await session.get(VerificationSession, record.id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(SessionBrowserMismatch):
        await session_repository.claim_callback("expired-callback", browser_token="foreign")
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "AWAITING_RIOT"
    assert stored.completed_at is None and stored.error_code is None
    with pytest.raises(SessionExpired):
        await session_repository.claim_callback("expired-callback", browser_token="correct")
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "EXPIRED"
    assert stored.completed_at is not None and stored.error_code == "SESSION_EXPIRED"


@pytest.mark.parametrize("entrypoint", ["start", "callback"])
async def test_used_session_is_never_rewritten_as_expired(
    session_repository: VerificationSessionRepository,
    entrypoint: str,
) -> None:
    created, record = await authorize_session(
        session_repository, guild_id=123, user_id=456, state="used-state"
    )
    async with session_repository._sessions.begin() as session:
        stored = await session.get(VerificationSession, record.id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(SessionAlreadyUsed):
        if entrypoint == "start":
            await session_repository.begin_oauth(
                token=created.token, state="another-state", browser_token="test-browser"
            )
        else:
            await session_repository.claim_callback("used-state", browser_token="test-browser")
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "PROCESSING_RIOT"
    assert stored.completed_at is None and stored.error_code is None


async def test_retry_cap_pauses_once_retains_link_and_requires_explicit_resume(
    session_repository: VerificationSessionRepository,
) -> None:
    created, claimed = await reserve_completion(session_repository)
    assert not await session_repository.retry_discord(
        claimed.id, error_code="RETRY", delay_seconds=3600, max_attempts=2
    )
    assert await session_repository.claim_pending() == []
    # A duplicate retry without a cap preserves the already scheduled backoff.
    await session_repository.retry_discord(claimed.id, error_code="RETRY", delay_seconds=0)
    assert await session_repository.claim_pending() == []
    async with session_repository._sessions.begin() as session:
        due = await session.get(VerificationSession, claimed.id)
        assert due is not None
        due.next_attempt_at = datetime.now(UTC)
    assert [r.id for r in await session_repository.claim_pending()] == [claimed.id]
    assert await session_repository.retry_discord(
        claimed.id, error_code="RETRY_CAP", delay_seconds=0, max_attempts=2
    )
    assert not await session_repository.retry_discord(
        claimed.id, error_code="SECOND_CALL", delay_seconds=0, max_attempts=2
    )
    async with session_repository._sessions.begin() as session:
        stored = await session.get(VerificationSession, claimed.id)
        assert stored is not None
        stored.completed_at = datetime.now(UTC) - timedelta(days=10)
        stored.expires_at = datetime.now(UTC) - timedelta(days=10)
    restarted = VerificationSessionRepository(session_repository._sessions)
    assert await restarted.expire_and_purge(retention_days=7) == (0, 0)
    assert await restarted.claim_pending(method=None) == []
    assert await restarted.claim_audit_reconciliations() == []
    stored = await restarted.get_by_start_token(created.token)
    assert stored is not None and stored.status == "DISCORD_RETRY_PAUSED"
    assert stored.error_code == "RETRY_CAP" and stored.next_attempt_at is None
    assert stored.completion_attempts == 2
    async with restarted._sessions() as session:
        assert await session.get(VerificationLink, (123, 456)) is not None
    with pytest.raises(SessionAlreadyUsed):
        await restarted.create(guild_id=123, user_id=456, ttl_seconds=600)
    assert await restarted.resume_paused_for_user(123, 999) is None
    assert await restarted.resume_paused_for_user(123, 456) == claimed.id
    assert not await restarted.resume_paused_for_user(123, 456)
    stored = await restarted.get_by_start_token(created.token)
    assert stored is not None and stored.status == "VERIFIED_PENDING_DISCORD"
    assert stored.completion_attempts == 0 and stored.error_code is None
    assert [r.id for r in await restarted.claim_pending()] == [claimed.id]


@pytest.mark.parametrize("changed", ["puuid", "platform", "generation", "deleting", "absent"])
async def test_resume_paused_cancels_stale_session_preserves_replacement_link(
    session_repository: VerificationSessionRepository,
    changed: str,
) -> None:
    created, claimed = await reserve_completion(session_repository)
    assert await session_repository.retry_discord(
        claimed.id, error_code="RETRY", delay_seconds=0, max_attempts=1
    )
    async with session_repository._sessions.begin() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert link is not None
        if changed == "puuid":
            link.puuid = "replacement"
        elif changed == "platform":
            link.platform = "EUW1"
        elif changed == "generation":
            link.created_at += timedelta(seconds=1)
        elif changed == "deleting":
            link.deletion_requested_at = datetime.now(UTC)
        else:
            await session.delete(link)
    assert not await session_repository.resume_paused_for_user(123, 456)
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "CANCELLED"
    assert stored.error_code == "VERIFICATION_LINK_CHANGED"
    async with session_repository._sessions() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert (link is None) == (changed == "absent")
        if changed == "puuid":
            assert link is not None and link.puuid == "replacement"


async def test_paused_cancellation_preserves_audit_reconciliation(
    session_repository: VerificationSessionRepository,
) -> None:
    created, claimed = await reserve_completion(session_repository)
    async with session_repository._sessions.begin() as session:
        stored = await session.get(VerificationSession, claimed.id)
        assert stored is not None
        stored.audit_pending_channel_id = 654
    assert await session_repository.retry_discord(
        claimed.id, error_code="RETRY", delay_seconds=0, max_attempts=1
    )
    await session_repository.cancel_for_user(123, 456, created_at_or_before=claimed.created_at)
    assert not await session_repository.resume_paused_for_user(123, 456)
    assert not await session_repository.retry_discord(
        claimed.id, error_code="AUDIT_RETRY", delay_seconds=0, max_attempts=1
    )
    assert [r.id for r in await session_repository.claim_audit_reconciliations()] == [claimed.id]
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "CANCELLED"


@pytest.mark.parametrize("interrupt", ["timeout", "cancel"])
@pytest.mark.parametrize("cancel_after", [False, True])
async def test_audit_interruption_retains_intent_and_reconciles_accepted_message(
    session_repository: VerificationSessionRepository,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: str,
    cancel_after: bool,
) -> None:
    created, claimed = await reserve_completion(session_repository)
    monkeypatch.setattr(
        "moon_poro.verification_sessions.AUDIT_RECONCILIATION_TIMEOUT_SECONDS", 0.02
    )
    accepted: list[int] = []
    sent = asyncio.Event()

    async def blocked(record: VerificationSession, active: bool) -> list[int]:
        assert active and record.audit_pending_channel_id == 654
        accepted.append(987)
        sent.set()
        await asyncio.Event().wait()
        return accepted

    task = asyncio.create_task(
        session_repository.publish_audit(claimed.id, channel_id=654, operation=blocked)
    )
    await sent.wait()
    if interrupt == "cancel":
        task.cancel()
    with pytest.raises(TimeoutError if interrupt == "timeout" else asyncio.CancelledError):
        await task
    monkeypatch.setattr("moon_poro.verification_sessions.AUDIT_RECONCILIATION_TIMEOUT_SECONDS", 10)
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.audit_pending_channel_id == 654
    assert stored.status == "APPLYING_DISCORD"
    if cancel_after:
        await session_repository.cancel_for_user(123, 456, created_at_or_before=claimed.created_at)
        async with session_repository._sessions.begin() as session:
            link = await session.get(VerificationLink, (123, 456), with_for_update=True)
            assert link is not None
            await session.delete(link)

    async def reconcile(record: VerificationSession, active: bool) -> list[int]:
        assert active is not cancel_after
        assert record.audit_pending_channel_id == 654
        return accepted

    restarted = VerificationSessionRepository(session_repository._sessions)
    assert (
        await restarted.publish_audit(claimed.id, channel_id=654, operation=reconcile)
        is not cancel_after
    )
    assert accepted == [987]
    stored = await restarted.get_by_start_token(created.token)
    assert stored is not None and stored.audit_pending_channel_id is None
    if cancel_after:
        cleanup = await restarted.claim_audit_cleanups(limit=5, claim_timeout_seconds=300)
        assert [(item.channel_id, item.message_id) for item in cleanup] == [(654, 987)]
    else:
        async with restarted._sessions() as session:
            link = await session.get(VerificationLink, (123, 456))
            assert link is not None and link.message_id == 987


@pytest.mark.parametrize("generation_delta", [0, 1])
@pytest.mark.parametrize("deleting", [False, True])
async def test_fail_discord_real_roundtrip_generation_and_deletion_guards(
    session_repository: VerificationSessionRepository,
    generation_delta: int,
    deleting: bool,
) -> None:
    _, claimed = await reserve_completion(session_repository)
    async with session_repository._sessions.begin() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert link is not None
        link.created_at += timedelta(seconds=generation_delta)
        if deleting:
            link.deletion_requested_at = datetime.now(UTC)
    await session_repository.fail_discord(claimed.id, "FAILED")
    async with session_repository._sessions() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert (link is not None) == (bool(generation_delta) or deleting)


@pytest.mark.parametrize("generation_delta", [0, 1])
@pytest.mark.parametrize("offset_minutes", [-300, 120])
async def test_fail_discord_accepts_driver_offset_but_preserves_new_generation(
    session_repository: VerificationSessionRepository,
    generation_delta: int,
    offset_minutes: int,
) -> None:
    _, claimed = await reserve_completion(session_repository)
    async with session_repository._sessions.begin() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert link is not None
        link.created_at += timedelta(seconds=generation_delta)

    def driver_offset(_session: Session, instance: object) -> None:
        # Emulate an aware driver returning a non-UTC offset while the session
        # generation still has SQLite's naive-UTC round-trip representation.
        if isinstance(instance, VerificationLink):
            set_committed_value(
                instance,
                "created_at",
                as_utc(instance.created_at).astimezone(timezone(timedelta(minutes=offset_minutes))),
            )

    event.listen(Session, "loaded_as_persistent", driver_offset)
    try:
        await session_repository.fail_discord(claimed.id, "FAILED")
    finally:
        event.remove(Session, "loaded_as_persistent", driver_offset)
    async with session_repository._sessions() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert (link is not None) == bool(generation_delta)


async def test_pending_at_retry_cap_pauses_without_a_new_worker_claim(
    session_repository: VerificationSessionRepository,
) -> None:
    _, claimed = await reserve_completion(session_repository)
    assert not await session_repository.retry_discord(
        claimed.id, error_code="RETRY", delay_seconds=3600
    )
    assert await session_repository.retry_discord(
        claimed.id, error_code="PAUSED", delay_seconds=0, max_attempts=1
    )
    assert await session_repository.claim_pending() == []


async def test_bound_browser_can_restart_oauth_and_only_newest_state_works(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    await session_repository.begin_oauth(
        token=created.token, state="first-state", browser_token="bound-browser"
    )
    restarted = await session_repository.begin_oauth(
        token=created.token, state="second-state", browser_token="bound-browser"
    )

    assert restarted.status == "AWAITING_RIOT"
    assert restarted.oauth_state_hash == hash_secret("second-state")
    with pytest.raises(SessionNotFound):
        await session_repository.claim_callback("first-state", browser_token="bound-browser")
    claimed = await session_repository.claim_callback("second-state", browser_token="bound-browser")
    assert claimed.status == "PROCESSING_RIOT"
    # Once Riot answered, the start link cannot issue another state.
    with pytest.raises(SessionAlreadyUsed):
        await session_repository.begin_oauth(
            token=created.token, state="third-state", browser_token="bound-browser"
        )


@pytest.mark.parametrize("browser_token", ["", "foreign-browser"])
async def test_foreign_browser_cannot_restart_oauth(
    session_repository: VerificationSessionRepository, browser_token: str
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    await session_repository.begin_oauth(
        token=created.token, state="bound-state", browser_token="bound-browser"
    )

    with pytest.raises(SessionBrowserMismatch):
        await session_repository.begin_oauth(
            token=created.token, state="stolen-state", browser_token=browser_token
        )

    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "AWAITING_RIOT"
    assert stored.oauth_state_hash == hash_secret("bound-state")
    assert stored.browser_binding_hash == hash_secret("bound-browser")


async def test_restart_after_expiry_is_persisted_only_for_bound_browser(
    session_repository: VerificationSessionRepository,
) -> None:
    created = await session_repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    record = await session_repository.begin_oauth(
        token=created.token, state="old-state", browser_token="bound-browser"
    )
    async with session_repository._sessions.begin() as session:
        stored = await session.get(VerificationSession, record.id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(SessionBrowserMismatch):
        await session_repository.begin_oauth(
            token=created.token, state="new-state", browser_token="foreign-browser"
        )
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "AWAITING_RIOT"
    with pytest.raises(SessionExpired):
        await session_repository.begin_oauth(
            token=created.token, state="new-state", browser_token="bound-browser"
        )
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "EXPIRED"
    assert stored.oauth_state_hash == hash_secret("old-state")


async def test_fail_with_expected_status_never_overwrites_reserved_link(
    session_repository: VerificationSessionRepository,
) -> None:
    created, record = await authorize_session(
        session_repository, guild_id=123, user_id=456, state="reserved-state"
    )
    assert (
        await session_repository.reserve_link(
            session_id=record.id,
            platform="EUN1",
            puuid="reserved-puuid",
            game_name="Moon",
            tag_line="EUNE",
        )
        == LinkReservationResult.RESERVED
    )

    await session_repository.fail(
        record.id, "INTERNAL_ERROR", expected_status=VerificationSessionStatus.PROCESSING_RIOT.value
    )
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "VERIFIED_PENDING_DISCORD"

    other, processing = await authorize_session(
        session_repository, guild_id=123, user_id=789, state="processing-state"
    )
    await session_repository.fail(
        processing.id,
        "INTERNAL_ERROR",
        expected_status=VerificationSessionStatus.PROCESSING_RIOT.value,
    )
    failed = await session_repository.get_by_start_token(other.token)
    assert failed is not None and (failed.status, failed.error_code) == (
        "FAILED",
        "INTERNAL_ERROR",
    )


async def _publish_completed(
    repository: VerificationSessionRepository,
) -> tuple[CreatedVerificationSession, VerificationSession]:
    created, claimed = await reserve_completion(repository)

    async def publish(_record: VerificationSession, active: bool) -> list[int]:
        assert active
        return [987]

    assert await repository.publish_audit(claimed.id, channel_id=654, operation=publish)
    return created, claimed


async def test_completion_queues_one_confirmation_and_lease_hides_it(
    session_repository: VerificationSessionRepository,
) -> None:
    created, claimed = await _publish_completed(session_repository)
    assert created.session_id == claimed.id
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.status == "COMPLETED"
    assert stored.confirmation_due_at is not None

    due = await session_repository.claim_confirmations(limit=5, lease_seconds=30)
    assert [(record.id, link.puuid) for record, link in due] == [(claimed.id, "puuid")]
    # A delivery in progress keeps the row until its lease runs out.
    assert await session_repository.claim_confirmations(limit=5, lease_seconds=30) == []

    await session_repository.finish_confirmation(claimed.id)
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.confirmation_due_at is None


async def test_abandoned_confirmation_claim_returns_after_its_lease(
    session_repository: VerificationSessionRepository,
) -> None:
    _created, claimed = await _publish_completed(session_repository)
    first = await session_repository.claim_confirmations(limit=5, lease_seconds=0)
    restarted = VerificationSessionRepository(session_repository._sessions)
    second = await restarted.claim_confirmations(limit=5, lease_seconds=30)
    assert [record.id for record, _link in first] == [record.id for record, _link in second]
    assert [record.id for record, _link in second] == [claimed.id]


@pytest.mark.parametrize("changed", ["deleting", "generation", "absent"])
async def test_confirmation_is_dropped_when_the_link_changed(
    session_repository: VerificationSessionRepository,
    changed: str,
) -> None:
    created, _claimed = await _publish_completed(session_repository)
    async with session_repository._sessions.begin() as session:
        link = await session.get(VerificationLink, (123, 456))
        assert link is not None
        if changed == "deleting":
            link.deletion_requested_at = datetime.now(UTC)
        elif changed == "generation":
            link.created_at += timedelta(seconds=1)
        else:
            await session.delete(link)

    assert await session_repository.claim_confirmations(limit=5, lease_seconds=0) == []
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.confirmation_due_at is None


async def test_cancelled_completion_queues_no_confirmation(
    session_repository: VerificationSessionRepository,
) -> None:
    created, claimed = await reserve_completion(session_repository)
    await session_repository.cancel_for_user(123, 456, created_at_or_before=claimed.created_at)

    async def publish(_record: VerificationSession, active: bool) -> list[int]:
        assert not active
        return []

    assert not await session_repository.publish_audit(claimed.id, channel_id=654, operation=publish)
    stored = await session_repository.get_by_start_token(created.token)
    assert stored is not None and stored.confirmation_due_at is None
    assert await session_repository.claim_confirmations(limit=5, lease_seconds=0) == []
