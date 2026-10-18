from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from moon_poro.database import Database, make_database_url, upgrade_database
from moon_poro.models import (
    Base,
    VerificationLink,
    VerificationMarkerCleanup,
    VerificationSession,
    VerificationSessionStatus,
    Warning,
    WarningStatus,
)
from moon_poro.rank_refresh import RankRefreshDecision, RankSnapshot
from moon_poro.repositories import (
    GuildFeatureRepository,
    ModerationStatsRepository,
    RankRefreshRequestStatus,
    VerificationDeletionAccessLog,
    VerificationDeletionPolicy,
    VerificationDeletionProcessStatus,
    VerificationDeletionRequestStatus,
    VerificationLinkIdentity,
    VerificationRepository,
    VerificationSnapshotChanged,
    WarningRepository,
)
from moon_poro.settings import Settings
from moon_poro.time_utils import timestamps_match
from moon_poro.verification_sessions import (
    LinkReservationResult,
    SessionAlreadyUsed,
    SessionExpired,
    VerificationSessionRepository,
)


@pytest.mark.parametrize("reservation_first", [False, True])
async def test_rso_start_and_reservation_serialize_on_postgres(
    settings_factory: Callable[..., Settings],
    reservation_first: bool,
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),  # pragma: allowlist secret
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    database = Database(settings)
    repository = VerificationSessionRepository(database.session_factory)
    try:
        created = await repository.create(guild_id=guild_id, user_id=456, ttl_seconds=600)
        await repository.begin_oauth(token=created.token, state="state", browser_token="browser")
        record = await repository.claim_callback("state", browser_token="browser")
        entered = asyncio.Event()
        release = asyncio.Event()
        original_lock = repository._lock_user

        async def delayed_lock(session, guild, user):
            await original_lock(session, guild, user)
            entered.set()
            await release.wait()

        first = VerificationSessionRepository(database.session_factory)
        first._lock_user = delayed_lock

        async def reserve(repo):
            return await repo.reserve_link(
                session_id=record.id,
                platform="EUN1",
                puuid=f"race-{guild_id}",
                game_name="Moon",
                tag_line="EUNE",
            )

        async def start(repo):
            return await repo.create(guild_id=guild_id, user_id=456, ttl_seconds=600)

        first_task = asyncio.create_task(reserve(first) if reservation_first else start(first))
        await asyncio.wait_for(entered.wait(), timeout=3)
        second_task = asyncio.create_task(
            start(repository) if reservation_first else reserve(repository)
        )
        try:
            await asyncio.sleep(0.05)
            assert not second_task.done()
        finally:
            release.set()
        results = await asyncio.wait_for(
            asyncio.gather(first_task, second_task, return_exceptions=True), timeout=5
        )
        assert isinstance(results[1], SessionAlreadyUsed)
        saved = await repository.get_by_start_token(created.token)
        assert saved is not None
        if reservation_first:
            assert results[0] == LinkReservationResult.RESERVED
            assert saved.status == "VERIFIED_PENDING_DISCORD"
            assert len(await repository.claim_pending()) == 1
            publishing = asyncio.Event()
            finish_publish = asyncio.Event()

            async def publish(_record, active):
                assert active
                publishing.set()
                await finish_publish.wait()
                return [987]

            publication = asyncio.create_task(
                repository.publish_audit(record.id, channel_id=654, operation=publish)
            )
            await asyncio.wait_for(publishing.wait(), timeout=3)
            cancellation = asyncio.create_task(
                repository.cancel_for_user(
                    guild_id,
                    456,
                    created_at_or_before=saved.created_at,
                )
            )
            try:
                await asyncio.sleep(0.05)
                assert not cancellation.done()
            finally:
                finish_publish.set()
            assert await asyncio.wait_for(publication, timeout=5)
            await asyncio.wait_for(cancellation, timeout=5)
            async with database.session() as session:
                link = await session.get(VerificationLink, (guild_id, 456))
                assert link is not None and link.message_id == 987
                assert link.audit_channel_id == 654
        else:
            assert saved.status == "CANCELLED"
            async with database.session() as session:
                assert await session.get(VerificationLink, (guild_id, 456)) is None
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM verification_links WHERE guild_id=:guild"), {"guild": guild_id}
            )
            await connection.execute(
                text("DELETE FROM verification_sessions WHERE guild_id=:guild"), {"guild": guild_id}
            )
        await database.close()


async def test_paused_completion_expiry_and_audit_deadline_against_postgres(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),  # pragma: allowlist secret
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    database = Database(settings)
    repository = VerificationSessionRepository(database.session_factory)
    peer = VerificationSessionRepository(database.session_factory)
    links = VerificationRepository(database.session_factory)
    try:
        expired = await repository.create(guild_id=guild_id, user_id=1, ttl_seconds=-1)
        with pytest.raises(SessionExpired):
            await repository.begin_oauth(
                token=expired.token, state="expired", browser_token="browser"
            )
        saved_expired = await repository.get_by_start_token(expired.token)
        assert saved_expired is not None and saved_expired.status == "EXPIRED"

        created = await repository.create(
            guild_id=guild_id,
            user_id=2,
            ttl_seconds=600,
            discord_username="test-user",
            discord_guild_name="Test server",
        )
        await repository.begin_oauth(token=created.token, state="valid", browser_token="browser")
        callback = await repository.claim_callback("valid", browser_token="browser")
        assert (
            await repository.reserve_link(
                session_id=callback.id,
                platform="EUN1",
                puuid=f"paused-{guild_id}",
                game_name="Moon",
                tag_line="Test",
            )
            == LinkReservationResult.RESERVED
        )
        for attempt in range(2):
            claimed = await repository.claim_pending()
            assert [item.id for item in claimed] == [callback.id]
            assert await repository.retry_discord(
                callback.id,
                error_code="DISCORD_AUDIT_UNAVAILABLE",
                delay_seconds=0,
                max_attempts=2,
            ) is (attempt == 1)
        paused = await repository.get_by_start_token(created.token)
        assert paused is not None and paused.status == "DISCORD_RETRY_PAUSED"
        assert paused.discord_username == "test-user" and paused.discord_guild_name == "Test server"
        assert await repository.claim_pending() == []
        assert await links.get_by_user(guild_id, 2) is not None
        resumed = await asyncio.wait_for(
            asyncio.gather(
                repository.resume_paused_for_user(guild_id, 2),
                peer.resume_paused_for_user(guild_id, 2),
            ),
            timeout=3,
        )
        assert sorted(resumed) == [False, True]

        assert len(await repository.claim_pending()) == 1
        monkeypatch.setattr(
            "moon_poro.verification_sessions.AUDIT_RECONCILIATION_TIMEOUT_SECONDS", 0.05
        )
        operation = AsyncMock(return_value=[987])
        async with database.session_factory.begin() as blocker:
            await blocker.get(VerificationLink, (guild_id, 2), with_for_update=True)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    repository.publish_audit(
                        callback.id,
                        channel_id=654,
                        operation=operation,
                    ),
                    timeout=2,
                )
        operation.assert_not_awaited()
        pending_audit = await repository.get_by_start_token(created.token)
        assert pending_audit is not None and pending_audit.audit_pending_channel_id == 654
        monkeypatch.setattr(
            "moon_poro.verification_sessions.AUDIT_RECONCILIATION_TIMEOUT_SECONDS", 10
        )
        assert await repository.publish_audit(callback.id, channel_id=654, operation=operation)

        # A persisted pause must not be resurrected when deletion wins before
        # the waiting resume obtains the session lock.
        second = await repository.create(guild_id=guild_id, user_id=3, ttl_seconds=600)
        await repository.begin_oauth(token=second.token, state="second", browser_token="browser")
        second_record = await repository.claim_callback("second", browser_token="browser")
        await repository.reserve_link(
            session_id=second_record.id,
            platform="EUN1",
            puuid=f"delete-paused-{guild_id}",
            game_name="Moon",
            tag_line="Test",
        )
        assert len(await repository.claim_pending()) == 1
        assert await repository.retry_discord(
            second_record.id,
            error_code="DISCORD_ROLES_UNAVAILABLE",
            delay_seconds=0,
            max_attempts=1,
        )
        link = await links.get_by_user(guild_id, 3)
        assert link is not None
        async with database.session_factory.begin() as blocker:
            await blocker.get(VerificationSession, second_record.id, with_for_update=True)
            waiting_resume = asyncio.create_task(peer.resume_paused_for_user(guild_id, 3))
            await asyncio.sleep(0.05)
            assert not waiting_resume.done()
            deleted = await links.request_verification_deletion_with_identity(
                guild_id,
                3,
                identity=VerificationLinkIdentity.from_link(link),
                policy=VerificationDeletionPolicy.USER,
            )
            assert deleted.status is VerificationDeletionRequestStatus.REQUESTED
        assert not await asyncio.wait_for(waiting_resume, timeout=3)
        saved = await repository.get_by_start_token(second.token)
        assert saved is not None and saved.status == VerificationSessionStatus.CANCELLED.value
        retained = await links.get_by_user(guild_id, 3)
        assert retained is not None and retained.deletion_requested_at is not None
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM verification_links WHERE guild_id=:guild"), {"guild": guild_id}
            )
            await connection.execute(
                text("DELETE FROM verification_sessions WHERE guild_id=:guild"), {"guild": guild_id}
            )
        await database.close()


ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    "TEST_POSTGRES_PORT" not in os.environ,
    reason="requires an isolated PostgreSQL test database",
)


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


async def test_migrations_and_repositories_against_postgres(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )

    await upgrade_database(settings)
    connection = Database(settings)
    try:
        async with connection.engine.connect() as database_connection:
            table_names = await database_connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            warning_columns = await database_connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("warnings")
                }
            )
            warning_indexes = await database_connection.run_sync(
                lambda sync_connection: {
                    index["name"] for index in inspect(sync_connection).get_indexes("warnings")
                }
            )
            verification_columns = await database_connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("verification_links")
                }
            )
            verification_indexes = await database_connection.run_sync(
                lambda sync_connection: {
                    index["name"]
                    for index in inspect(sync_connection).get_indexes("verification_links")
                }
            )
            verification_session_columns = await database_connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("verification_sessions")
                }
            )
        assert set(Base.metadata.tables) <= table_names
        assert {"role_sync_pending", "audit_sync_pending"} <= warning_columns
        assert "ix_warnings_pending_sync" in warning_indexes
        assert {
            "riot_game_name",
            "riot_tag_line",
            "last_known_division",
            "last_known_league_points",
            "last_known_wins",
            "last_known_losses",
            "last_known_inactive",
            "rank_schedule_class",
            "rank_schedule_reason",
            "rank_role_sync_pending",
            "rank_user_refresh_requested_at",
            "deletion_requested_at",
            "deletion_next_attempt_at",
            "deletion_remove_rank_region_roles",
            "audit_channel_id",
        } <= verification_columns
        assert "ix_verification_links_rank_role_sync_due" in verification_indexes
        assert "ix_verification_links_deletion_due" in verification_indexes
        assert "verification_link_created_at" in verification_session_columns
        assert "browser_binding_hash" in verification_session_columns
        assert "verification_method" in verification_session_columns
        assert "discord_username" in verification_session_columns
        assert "discord_guild_name" in verification_session_columns
        assert "audit_pending_channel_id" in verification_session_columns
        assert "verification_audit_cleanups" in table_names
        assert "verification_marker_cleanups" in table_names

        verifications = VerificationRepository(connection.session_factory)
        created = await verifications.create(
            guild_id=guild_id,
            user_id=101,
            message_id=201,
            audit_channel_id=301,
            platform="EUN1",
            puuid="integration-puuid",
            riot_game_name="Moon Poro",
            riot_tag_line="EUNE",
        )
        assert await verifications.get_by_user(guild_id, 101) is not None
        assert (await verifications.get_by_puuid(guild_id, "integration-puuid")).message_id == 201
        assert (created.riot_game_name, created.riot_tag_line) == ("Moon Poro", "EUNE")
        assert [link.discord_user_id for link in await verifications.list_for_guild(guild_id)] == [
            101
        ]
        queue = await verifications.rank_refresh_queue_stats(guild_id)
        assert queue.due_count == 1
        assert queue.oldest_due_at is not None
        due_refreshes = await verifications.claim_due_rank_refreshes(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [link.discord_user_id for link in due_refreshes] == [101]
        assert (
            await verifications.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )
            == []
        )
        async with connection.session_factory.begin() as session:
            claimed = await session.get(VerificationLink, (guild_id, 101), with_for_update=True)
            assert claimed is not None
            claimed.rank_refresh_claimed_at = datetime.now(UTC) - timedelta(seconds=301)
        reclaimed = await verifications.claim_due_rank_refreshes(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [link.discord_user_id for link in reclaimed] == [101]
        reclaimed_at = reclaimed[0].rank_refresh_claimed_at
        assert reclaimed_at is not None
        assert await verifications.sync_riot_id_if_current(
            guild_id,
            101,
            game_name="Renamed Moon",
            tag_line="EUW",
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
            expected_claimed_at=reclaimed_at,
        )
        synced = await verifications.get_by_user(guild_id, 101)
        assert synced is not None
        assert (synced.riot_game_name, synced.riot_tag_line) == ("Renamed Moon", "EUW")
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 1
        retry_delay = await verifications.retry_rank_refresh(
            guild_id,
            101,
            base_delay_seconds=300,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
            expected_claimed_at=reclaimed_at,
        )
        assert retry_delay is not None and 240 <= retry_delay <= 360
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 0
        protected = await verifications.request_rank_refresh(
            guild_id,
            101,
            cooldown_seconds=3600,
            source="role_tamper",
        )
        assert protected.status == RankRefreshRequestStatus.BACKOFF_ACTIVE
        protected_again = await verifications.request_rank_refresh(
            guild_id,
            101,
            cooldown_seconds=3600,
            source="role_tamper",
        )
        assert protected_again.status == RankRefreshRequestStatus.COOLDOWN
        assert (
            await verifications.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )
            == []
        )
        assert await verifications.schedule_rank_refresh_now(guild_id, 101)
        assert (
            await verifications.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )
            == []
        )
        async with connection.session_factory.begin() as session:
            stored = await session.get(
                VerificationLink,
                (guild_id, 101),
                with_for_update=True,
            )
            assert stored is not None
            stored.rank_next_refresh_at = datetime.now(UTC)
        defer_claims = await verifications.claim_due_rank_refreshes(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [link.discord_user_id for link in defer_claims] == [101]
        defer_claimed_at = defer_claims[0].rank_refresh_claimed_at
        assert defer_claimed_at is not None
        assert await verifications.defer_rank_refresh(
            guild_id,
            101,
            delay_seconds=0,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
            expected_claimed_at=defer_claimed_at,
        )
        completion_claims = await verifications.claim_due_rank_refreshes(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [link.discord_user_id for link in completion_claims] == [101]
        completion_claimed_at = completion_claims[0].rank_refresh_claimed_at
        assert completion_claimed_at is not None
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 1
        assert await verifications.complete_rank_refresh(
            guild_id,
            101,
            rank_tier="EMERALD",
            refresh_interval_hours=24,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
            expected_claimed_at=completion_claimed_at,
        )
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 0
        refreshed = await verifications.get_by_user(guild_id, 101)
        assert refreshed is not None
        assert refreshed.last_known_rank == "EMERALD"
        assert refreshed.rank_refresh_failures == 0
        assert refreshed.rank_last_checked_at is not None
        assert refreshed.rank_refresh_claimed_at is None
        request = await verifications.request_rank_refresh(
            guild_id,
            101,
            cooldown_seconds=1800,
            source="user",
        )
        assert request.status == RankRefreshRequestStatus.ENQUEUED
        duplicate = await verifications.request_rank_refresh(
            guild_id,
            101,
            cooldown_seconds=1800,
            source="user",
        )
        assert duplicate.status == RankRefreshRequestStatus.COOLDOWN

        stored = await verifications.record_rank_snapshot(
            guild_id,
            101,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
            expected_claimed_at=None,
            expected_rank_last_checked_at=refreshed.rank_last_checked_at,
            decision=RankRefreshDecision(
                snapshot=RankSnapshot("DIAMOND", "IV", 20, 120, 100, False),
                interval_seconds=21_600,
                schedule_class="6h",
                reason="tier_changed",
                activity_observed=True,
                tier_changed=True,
                counter_reset=False,
                unranked_confirmations=0,
            ),
            next_interval_seconds=20_000,
        )
        assert stored
        role_syncs = await verifications.claim_due_rank_role_syncs(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [link.discord_user_id for link in role_syncs] == [101]
        original_role_snapshot_at = role_syncs[0].rank_last_checked_at
        newer_snapshot_at = await verifications.record_rank_snapshot(
            guild_id,
            101,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
            expected_claimed_at=None,
            expected_rank_last_checked_at=stored,
            decision=RankRefreshDecision(
                snapshot=RankSnapshot("DIAMOND", "III", 40, 121, 100, False),
                interval_seconds=43_200,
                schedule_class="12h",
                reason="activity_observed",
                activity_observed=True,
                tier_changed=False,
                counter_reset=False,
                unranked_confirmations=0,
            ),
            next_interval_seconds=40_000,
        )
        assert newer_snapshot_at is not None
        assert not await verifications.acknowledge_rank_role_sync(
            guild_id,
            101,
            expected_rank_last_checked_at=original_role_snapshot_at,
        )
        newer_role_syncs = await verifications.claim_due_rank_role_syncs(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [link.discord_user_id for link in newer_role_syncs] == [101]
        assert await verifications.acknowledge_rank_role_sync(
            guild_id,
            101,
            expected_rank_last_checked_at=newer_snapshot_at,
        )
        refreshed = await verifications.get_by_user(guild_id, 101)
        assert refreshed is not None
        assert refreshed.last_known_division == "III"
        assert refreshed.last_known_league_points == 40
        assert refreshed.rank_tier_change_count == 1
        assert not refreshed.rank_role_sync_pending
        stale_role_operation = AsyncMock()
        with pytest.raises(VerificationSnapshotChanged):
            await verifications.run_verification_role_update_with_identity(
                guild_id,
                101,
                identity=VerificationLinkIdentity.from_link(refreshed),
                expected_rank_last_checked_at=original_role_snapshot_at,
                operation=stale_role_operation,
            )
        stale_role_operation.assert_not_awaited()
        current_role_operation = AsyncMock()
        assert await verifications.run_verification_role_update_with_identity(
            guild_id,
            101,
            identity=VerificationLinkIdentity.from_link(refreshed),
            expected_rank_last_checked_at=newer_snapshot_at,
            operation=current_role_operation,
        )
        current_role_operation.assert_awaited_once()
        await verifications.log_access(
            guild_id=guild_id,
            actor_id=501,
            reason="integration test",
            discord_user_id=101,
            puuid=created.puuid,
        )
        assert await verifications.purge_access_logs(guild_id, retention_days=0) == 1

        rso_sessions = VerificationSessionRepository(connection.session_factory)
        rso = await rso_sessions.create(guild_id=guild_id, user_id=102, ttl_seconds=600)
        oauth_state = f"oauth-state-{guild_id}"
        await rso_sessions.begin_oauth(
            token=rso.token, state=oauth_state, browser_token="integration-browser"
        )
        callback = await rso_sessions.claim_callback(
            oauth_state, browser_token="integration-browser"
        )
        assert (
            await rso_sessions.reserve_link(
                session_id=callback.id,
                platform="EUN1",
                puuid="rso-integration-puuid",
                game_name="Moon",
                tag_line="EUNE",
            )
            == LinkReservationResult.RESERVED
        )
        pending = await rso_sessions.claim_pending()
        assert [item.discord_user_id for item in pending] == [102]
        await rso_sessions.complete_discord(
            pending[0].id,
            message_id=202,
            channel_id=303,
        )
        completed = await rso_sessions.get_by_start_token(rso.token)
        assert completed is not None and completed.status == "COMPLETED"
        assert (await verifications.get_by_user(guild_id, 102)).message_id == 202

        features = GuildFeatureRepository(connection.session_factory)
        assert not await features.get(guild_id, "feature", default=False)
        await features.set(guild_id, "feature", True, actor_id=501)
        assert await features.get(guild_id, "feature", default=False)

        warnings = WarningRepository(connection.session_factory)
        durations = {1: 7, 2: 14, 3: 3}
        first = await warnings.issue(
            guild_id=guild_id,
            user_id=101,
            requested_level=1,
            reasons="1",
            description="first",
            moderator_id=501,
            message_id=301,
            duration_by_level=durations,
        )
        second = await warnings.issue(
            guild_id=guild_id,
            user_id=101,
            requested_level=1,
            reasons="2",
            description="second",
            moderator_id=502,
            message_id=301,
            duration_by_level=durations,
        )
        assert second.level == 2
        assert second.parent_id == first.id
        active = await warnings.get_active(guild_id, 101)
        assert active is not None and active.id == second.id
        assert {item.moderator_id for item in active.moderators} == {501, 502}

        reverted = await warnings.revert(guild_id, 101)
        assert reverted is not None
        current, previous = reverted
        assert current.id == second.id
        assert previous is not None and previous.id == first.id
        assert not current.audit_sync_pending
        assert previous.audit_sync_pending

        stats = await ModerationStatsRepository(connection.session_factory).list_for_guild(guild_id)
        assert {(row.moderator_id, row.warnings_count) for row in stats} == {(501, 1), (502, 1)}

        removed = await _delete_verification_link(verifications, guild_id, 101)
        assert removed is not None and removed.puuid == "integration-puuid"
        assert await verifications.get_by_user(guild_id, 101) is None
        await _delete_verification_link(verifications, guild_id, 102)
    finally:
        await connection.close()


async def test_stale_riot_response_cannot_overwrite_reverified_link(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    repository = VerificationRepository(connection.session_factory)
    try:
        old = await repository.create(
            guild_id=guild_id,
            user_id=701,
            message_id=801,
            audit_channel_id=901,
            platform="EUN1",
            puuid="same-puuid",
            rank_snapshot=RankSnapshot("GOLD", "I", 90, 10, 5, False),
        )
        await _delete_verification_link(repository, guild_id, 701)
        await asyncio.sleep(0.002)
        new = await repository.create(
            guild_id=guild_id,
            user_id=701,
            message_id=802,
            audit_channel_id=901,
            platform="EUN1",
            puuid="same-puuid",
            rank_snapshot=RankSnapshot("SILVER", "IV", 10, 2, 3, False),
        )

        recorded = await repository.record_rank_snapshot(
            guild_id,
            701,
            expected_puuid=old.puuid or "",
            expected_platform=old.platform,
            expected_created_at=old.created_at,
            expected_claimed_at=None,
            expected_rank_last_checked_at=old.rank_last_checked_at,
            decision=RankRefreshDecision(
                snapshot=RankSnapshot("DIAMOND", "IV", 20, 100, 90, False),
                interval_seconds=21_600,
                schedule_class="6h",
                reason="tier_changed",
                activity_observed=True,
                tier_changed=True,
                counter_reset=False,
                unranked_confirmations=0,
            ),
            next_interval_seconds=21_600,
        )

        assert recorded is None
        current = await repository.get_by_user(guild_id, 701)
        assert current is not None
        assert current.created_at == new.created_at
        assert current.last_known_rank == "SILVER"
    finally:
        await _delete_verification_link(repository, guild_id, 701)
        await connection.close()


async def test_rank_refresh_claim_reclaim_fences_stale_worker_and_unclaimed_writer(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    repository = VerificationRepository(connection.session_factory)
    competing_repository = VerificationRepository(connection.session_factory)
    try:
        link = await repository.create(
            guild_id=guild_id,
            user_id=704,
            message_id=804,
            audit_channel_id=904,
            platform="EUN1",
            puuid="claim-reclaim-puuid",
        )
        concurrent_claims = await asyncio.gather(
            repository.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            ),
            competing_repository.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            ),
        )
        first_claims = [claimed for batch in concurrent_claims for claimed in batch]
        assert [claimed.discord_user_id for claimed in first_claims] == [704]
        first_claimed_at = first_claims[0].rank_refresh_claimed_at
        assert first_claimed_at is not None

        async with connection.session_factory.begin() as session:
            stored = await session.get(
                VerificationLink,
                (guild_id, 704),
                with_for_update=True,
            )
            assert stored is not None
            stored.rank_refresh_claimed_at = first_claimed_at - timedelta(seconds=301)
        await asyncio.sleep(0.002)
        second_claims = await repository.claim_due_rank_refreshes(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [claimed.discord_user_id for claimed in second_claims] == [704]
        second_claimed_at = second_claims[0].rank_refresh_claimed_at
        assert second_claimed_at is not None
        assert not timestamps_match(first_claimed_at, second_claimed_at)
        second_next_refresh_at = second_claims[0].rank_next_refresh_at
        identity = {
            "expected_puuid": link.puuid or "",
            "expected_platform": link.platform,
            "expected_created_at": link.created_at,
        }
        stale_decision = RankRefreshDecision(
            snapshot=RankSnapshot("DIAMOND", "IV", 20, 100, 90, False),
            interval_seconds=21_600,
            schedule_class="6h",
            reason="tier_changed",
            activity_observed=True,
            tier_changed=True,
            counter_reset=False,
            unranked_confirmations=0,
        )

        assert (
            await repository.record_rank_snapshot(
                guild_id,
                704,
                expected_claimed_at=first_claimed_at,
                expected_rank_last_checked_at=link.rank_last_checked_at,
                decision=stale_decision,
                next_interval_seconds=21_600,
                **identity,
            )
            is None
        )
        assert (
            await repository.retry_rank_refresh(
                guild_id,
                704,
                base_delay_seconds=300,
                expected_claimed_at=first_claimed_at,
                **identity,
            )
            is None
        )
        assert not await repository.release_rank_refresh_claim(
            guild_id,
            704,
            expected_claimed_at=first_claimed_at,
            **identity,
        )
        assert not await repository.defer_rank_refresh(
            guild_id,
            704,
            delay_seconds=600,
            expected_claimed_at=first_claimed_at,
            **identity,
        )
        assert (
            await repository.record_rank_snapshot(
                guild_id,
                704,
                expected_claimed_at=None,
                expected_rank_last_checked_at=link.rank_last_checked_at,
                decision=stale_decision,
                next_interval_seconds=21_600,
                **identity,
            )
            is None
        )

        still_claimed = await repository.get_by_user(guild_id, 704)
        assert still_claimed is not None
        assert timestamps_match(still_claimed.rank_refresh_claimed_at, second_claimed_at)
        assert still_claimed.rank_next_refresh_at == second_next_refresh_at
        assert still_claimed.rank_refresh_failures == 0
        assert still_claimed.last_known_rank is None

        completed_at = await repository.record_rank_snapshot(
            guild_id,
            704,
            expected_claimed_at=second_claimed_at,
            expected_rank_last_checked_at=link.rank_last_checked_at,
            decision=stale_decision,
            next_interval_seconds=21_600,
            **identity,
        )
        assert completed_at is not None
        unclaimed_decision = RankRefreshDecision(
            snapshot=RankSnapshot("EMERALD", "II", 75, 23, 19, False),
            interval_seconds=43_200,
            schedule_class="12h",
            reason="activity_observed",
            activity_observed=True,
            tier_changed=True,
            counter_reset=False,
            unranked_confirmations=0,
        )
        stale_after_intervening_snapshot = await repository.record_rank_snapshot(
            guild_id,
            704,
            expected_claimed_at=None,
            expected_rank_last_checked_at=link.rank_last_checked_at,
            decision=unclaimed_decision,
            next_interval_seconds=43_200,
            **identity,
        )
        assert stale_after_intervening_snapshot is None
        unclaimed_at = await repository.record_rank_snapshot(
            guild_id,
            704,
            expected_claimed_at=None,
            expected_rank_last_checked_at=completed_at,
            decision=unclaimed_decision,
            next_interval_seconds=43_200,
            **identity,
        )

        assert unclaimed_at is not None
        completed = await repository.get_by_user(guild_id, 704)
        assert completed is not None
        assert completed.rank_refresh_claimed_at is None
        assert completed.last_known_rank == "EMERALD"
    finally:
        await _delete_verification_link(repository, guild_id, 704)
        await connection.close()


async def test_stale_claim_mutations_cannot_change_reverified_link(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    repository = VerificationRepository(connection.session_factory)
    try:
        old = await repository.create(
            guild_id=guild_id,
            user_id=703,
            message_id=801,
            audit_channel_id=901,
            platform="EUN1",
            puuid="same-claim-puuid",
            rank_snapshot=RankSnapshot("GOLD", "I", 90, 10, 5, False),
        )
        await _delete_verification_link(repository, guild_id, 703)
        await asyncio.sleep(0.002)
        new = await repository.create(
            guild_id=guild_id,
            user_id=703,
            message_id=802,
            audit_channel_id=901,
            platform="EUN1",
            puuid="same-claim-puuid",
            rank_snapshot=RankSnapshot("SILVER", "IV", 10, 2, 3, False),
        )
        claim_marker = datetime.now(UTC)
        async with connection.session_factory.begin() as session:
            stored = await session.get(VerificationLink, (guild_id, 703), with_for_update=True)
            assert stored is not None
            stored.rank_refresh_claimed_at = claim_marker
            stored.rank_role_sync_pending = True
            stored.rank_role_sync_claimed_at = claim_marker
            stored.rank_role_sync_next_attempt_at = claim_marker
            next_refresh = stored.rank_next_refresh_at

        identity = {
            "expected_puuid": old.puuid or "",
            "expected_platform": old.platform,
            "expected_created_at": old.created_at,
        }
        stale_claim_marker = claim_marker - timedelta(microseconds=1)
        stale_refresh = await repository.request_rank_refresh(
            guild_id,
            703,
            cooldown_seconds=1800,
            source="user",
            expected_puuid=old.puuid or "",
            expected_platform=old.platform,
            expected_created_at=old.created_at,
        )
        assert stale_refresh.status == RankRefreshRequestStatus.LINK_CHANGED
        stale_deletion = await repository.request_verification_deletion_with_identity(
            guild_id,
            703,
            identity=VerificationLinkIdentity.from_link(old),
            policy=VerificationDeletionPolicy.USER,
        )
        assert stale_deletion.status is VerificationDeletionRequestStatus.LINK_CHANGED
        assert (
            await repository.retry_rank_refresh(
                guild_id,
                703,
                base_delay_seconds=300,
                expected_claimed_at=stale_claim_marker,
                **identity,
            )
            is None
        )
        assert not await repository.release_rank_refresh_claim(
            guild_id,
            703,
            expected_claimed_at=stale_claim_marker,
            **identity,
        )
        assert not await repository.defer_rank_refresh(
            guild_id,
            703,
            delay_seconds=7 * 86_400,
            expected_claimed_at=stale_claim_marker,
            **identity,
        )
        assert not await repository.defer_rank_role_sync(
            guild_id,
            703,
            delay_seconds=7 * 86_400,
            expected_rank_last_checked_at=old.rank_last_checked_at,
            **identity,
        )

        current = await repository.get_by_user(guild_id, 703)
        assert current is not None
        assert current.created_at == new.created_at
        assert current.rank_user_refresh_requested_at is None
        assert current.deletion_requested_at is None
        assert current.rank_refresh_failures == 0
        assert current.rank_refresh_claimed_at == claim_marker
        assert current.rank_next_refresh_at == next_refresh
        assert current.rank_role_sync_claimed_at == claim_marker
        assert current.rank_role_sync_next_attempt_at == claim_marker
    finally:
        await _delete_verification_link(repository, guild_id, 703)
        await connection.close()


async def test_delete_tombstone_blocks_workers_and_survives_claim_restart(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    repository = VerificationRepository(connection.session_factory)
    try:
        link = await repository.create(
            guild_id=guild_id,
            user_id=702,
            message_id=803,
            audit_channel_id=901,
            platform="EUN1",
            puuid="delete-puuid",
            rank_snapshot=RankSnapshot("EMERALD", "II", 50, 20, 10, False),
        )
        identity = VerificationLinkIdentity.from_link(link)
        request = await repository.request_verification_deletion_with_identity(
            guild_id,
            702,
            identity=identity,
            policy=VerificationDeletionPolicy.USER,
        )
        assert request.status is VerificationDeletionRequestStatus.REQUESTED
        assert request.link is not None and request.link.deletion_requested_at is not None
        assert (
            await repository.claim_due_rank_refreshes(guild_id, limit=1, claim_timeout_seconds=300)
            == []
        )
        assert (
            await repository.claim_due_rank_role_syncs(guild_id, limit=1, claim_timeout_seconds=300)
            == []
        )
        assert (
            await repository.claim_due_verification_deletions(
                guild_id, limit=1, claim_timeout_seconds=300
            )
            == []
        )

        async with connection.session_factory.begin() as session:
            stored = await session.get(VerificationLink, (guild_id, 702), with_for_update=True)
            assert stored is not None
            stored.deletion_claimed_at = datetime.now(UTC) - timedelta(seconds=301)
        claimed = await repository.claim_due_verification_deletions(
            guild_id, limit=1, claim_timeout_seconds=300
        )
        assert [item.discord_user_id for item in claimed] == [702]
        claimed_at = claimed[0].deletion_claimed_at
        assert claimed_at is not None
        retry_operation = AsyncMock(return_value=False)
        retry_result = await repository.process_verification_deletion_with_identity(
            guild_id,
            702,
            identity=identity,
            expected_claimed_at=claimed_at,
            base_delay_seconds=60,
            operation=retry_operation,
        )
        assert retry_result.status is VerificationDeletionProcessStatus.RETRY_SCHEDULED
        assert retry_result.retry_after_seconds is not None
        retry_operation.assert_awaited_once_with()
        assert (
            await repository.claim_due_verification_deletions(
                guild_id, limit=1, claim_timeout_seconds=300
            )
            == []
        )
        async with connection.session_factory.begin() as session:
            stored = await session.get(VerificationLink, (guild_id, 702), with_for_update=True)
            assert stored is not None
            stored.deletion_next_attempt_at = datetime.now(UTC)
        claimed = await repository.claim_due_verification_deletions(
            guild_id, limit=1, claim_timeout_seconds=300
        )
        assert [item.discord_user_id for item in claimed] == [702]
        claimed_at = claimed[0].deletion_claimed_at
        assert claimed_at is not None
        delete_operation = AsyncMock(return_value=True)
        delete_result = await repository.process_verification_deletion_with_identity(
            guild_id,
            702,
            identity=identity,
            expected_claimed_at=claimed_at,
            base_delay_seconds=60,
            operation=delete_operation,
        )
        assert delete_result.status is VerificationDeletionProcessStatus.DELETED
        delete_operation.assert_awaited_once_with()
        assert await repository.get_by_user(guild_id, 702) is None

        cleanup_generation = await repository.enqueue_verified_marker_cleanup(guild_id, 702)
        marker_cleanup = await repository.claim_due_verified_marker_cleanups(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [item.discord_user_id for item in marker_cleanup] == [702]
        newer_generation = await repository.enqueue_verified_marker_cleanup(guild_id, 702)
        assert newer_generation == cleanup_generation + 1
        assert await repository.retry_verified_marker_cleanup(
            guild_id,
            702,
            expected_generation=newer_generation,
            base_delay_seconds=1,
        )
        assert not await repository.acknowledge_verified_marker_cleanup(
            guild_id,
            702,
            expected_generation=cleanup_generation,
        )
        assert (
            await repository.retry_verified_marker_cleanup(
                guild_id,
                702,
                expected_generation=cleanup_generation,
                base_delay_seconds=1,
            )
            is None
        )
        async with connection.session_factory.begin() as session:
            stored_cleanup = await session.get(
                VerificationMarkerCleanup,
                (guild_id, 702),
                with_for_update=True,
            )
            assert stored_cleanup is not None
            stored_cleanup.next_attempt_at = datetime.now(UTC)
        restarted_repository = VerificationRepository(connection.session_factory)
        marker_cleanup = await restarted_repository.claim_due_verified_marker_cleanups(
            guild_id,
            limit=1,
            claim_timeout_seconds=300,
        )
        assert [item.discord_user_id for item in marker_cleanup] == [702]
        assert marker_cleanup[0].generation == newer_generation
        assert await restarted_repository.acknowledge_verified_marker_cleanup(
            guild_id,
            702,
            expected_generation=newer_generation,
        )
        assert (
            await restarted_repository.claim_due_verified_marker_cleanups(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )
            == []
        )
    finally:
        await _delete_verification_link(repository, guild_id, 702)
        await connection.close()


async def test_strict_null_puuid_admin_deletion_lifecycle_against_postgres(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    repository = VerificationRepository(connection.session_factory)
    try:
        link = await repository.create(
            guild_id=guild_id,
            user_id=704,
            message_id=None,
            audit_channel_id=804,
            platform="EUN1",
            puuid=None,
        )
        identity = VerificationLinkIdentity.from_link(link)
        request = await repository.request_verification_deletion_with_identity(
            guild_id,
            704,
            identity=identity,
            policy=VerificationDeletionPolicy.ADMIN,
            access_log=VerificationDeletionAccessLog(
                actor_id=900,
                reason="Administracyjne usunięcie: " + "x" * 400,
            ),
        )
        repeated = await repository.request_verification_deletion_with_identity(
            guild_id,
            704,
            identity=identity,
            policy=VerificationDeletionPolicy.ADMIN,
            access_log=VerificationDeletionAccessLog(actor_id=900, reason="Powtórzenie"),
        )

        assert request.status is VerificationDeletionRequestStatus.REQUESTED
        assert repeated.status is VerificationDeletionRequestStatus.ALREADY_REQUESTED
        assert request.link is not None
        assert request.link.puuid is None
        assert request.link.audit_channel_id == 804
        # Admin removal keeps region and rank roles, like a member's own removal.
        assert not request.link.deletion_remove_rank_region_roles
        claimed_at = request.link.deletion_claimed_at
        assert claimed_at is not None
        stale_operation = AsyncMock(return_value=True)
        stale_result = await repository.process_verification_deletion_with_identity(
            guild_id,
            704,
            identity=identity,
            expected_claimed_at=claimed_at - timedelta(microseconds=1),
            base_delay_seconds=60,
            operation=stale_operation,
        )
        assert stale_result.status is VerificationDeletionProcessStatus.CLAIM_LOST
        stale_operation.assert_not_awaited()
        async with connection.engine.connect() as database_connection:
            logs = (
                await database_connection.execute(
                    text(
                        """
                        SELECT actor_id, discord_user_id, puuid, reason
                        FROM verification_access_logs
                        WHERE guild_id = :guild_id AND discord_user_id = 704
                        """
                    ),
                    {"guild_id": guild_id},
                )
            ).all()
        assert len(logs) == 1
        assert tuple(logs[0]) == (900, 704, None, "Administracyjne usunięcie: " + "x" * 273)
        delete_operation = AsyncMock(return_value=True)
        delete_result = await repository.process_verification_deletion_with_identity(
            guild_id,
            704,
            identity=identity,
            expected_claimed_at=claimed_at,
            base_delay_seconds=60,
            operation=delete_operation,
        )
        assert delete_result.status is VerificationDeletionProcessStatus.DELETED
        delete_operation.assert_awaited_once_with()
    finally:
        await _delete_verification_link(repository, guild_id, 704)
        async with connection.engine.begin() as database_connection:
            await database_connection.execute(
                text("DELETE FROM verification_access_logs WHERE guild_id = :guild_id"),
                {"guild_id": guild_id},
            )
        await connection.close()


async def test_guarded_deletion_holds_row_lock_through_side_effect(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    repository = VerificationRepository(connection.session_factory)
    competing_repository = VerificationRepository(connection.session_factory)
    release_operation = asyncio.Event()
    operation_started = asyncio.Event()
    competing_claim_started = asyncio.Event()
    side_effects: list[str] = []
    process_task: asyncio.Task[object] | None = None
    claim_task: asyncio.Task[object] | None = None
    try:
        link = await repository.create(
            guild_id=guild_id,
            user_id=705,
            message_id=None,
            platform="EUN1",
            puuid=None,
        )
        identity = VerificationLinkIdentity.from_link(link)
        request = await repository.request_verification_deletion_with_identity(
            guild_id,
            705,
            identity=identity,
            policy=VerificationDeletionPolicy.USER,
        )
        assert request.link is not None
        stale_claim = datetime.now(UTC) - timedelta(seconds=301)
        async with connection.session_factory.begin() as session:
            stored = await session.get(VerificationLink, (guild_id, 705), with_for_update=True)
            assert stored is not None
            stored.deletion_claimed_at = stale_claim
            stored.deletion_next_attempt_at = stale_claim

        async def operation() -> bool:
            side_effects.append("executed")
            operation_started.set()
            await release_operation.wait()
            return True

        async def competing_claim() -> list[VerificationLink]:
            competing_claim_started.set()
            return await competing_repository.claim_due_verification_deletions(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )

        process_task = asyncio.create_task(
            repository.process_verification_deletion_with_identity(
                guild_id,
                705,
                identity=identity,
                expected_claimed_at=stale_claim,
                base_delay_seconds=10,
                operation=operation,
            )
        )
        await asyncio.wait_for(operation_started.wait(), timeout=2)
        claim_task = asyncio.create_task(competing_claim())
        await asyncio.wait_for(competing_claim_started.wait(), timeout=2)
        competing_claims = await asyncio.wait_for(claim_task, timeout=2)

        assert competing_claims == []
        assert side_effects == ["executed"]
        release_operation.set()
        process_result = await asyncio.wait_for(process_task, timeout=2)

        assert process_result.status is VerificationDeletionProcessStatus.DELETED
        assert await repository.get_by_user(guild_id, 705) is None
    finally:
        release_operation.set()
        pending_tasks = [task for task in (process_task, claim_task) if task is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await _delete_verification_link(repository, guild_id, 705)
        await connection.close()


async def test_guarded_role_apply_serializes_admin_deletion_cleanup(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    await upgrade_database(settings)
    connection = Database(settings)
    role_repository = VerificationRepository(connection.session_factory)
    deletion_repository = VerificationRepository(connection.session_factory)
    role_apply_started = asyncio.Event()
    deletion_started = asyncio.Event()
    release_role_apply = asyncio.Event()
    effects: list[str] = []
    apply_task: asyncio.Task[object] | None = None
    deletion_task: asyncio.Task[object] | None = None
    try:
        link = await role_repository.create(
            guild_id=guild_id,
            user_id=706,
            message_id=806,
            audit_channel_id=807,
            platform="EUN1",
            puuid="role-fence-puuid",
        )
        identity = VerificationLinkIdentity.from_link(link)

        async def apply_roles() -> None:
            effects.append("role-apply-started")
            role_apply_started.set()
            await release_role_apply.wait()
            effects.append("role-apply-finished")

        async def request_admin_deletion() -> object:
            deletion_started.set()
            return await deletion_repository.request_verification_deletion_with_identity(
                guild_id,
                706,
                identity=identity,
                policy=VerificationDeletionPolicy.ADMIN,
                access_log=VerificationDeletionAccessLog(
                    actor_id=900,
                    reason="Test serializacji usuwania",
                ),
            )

        apply_task = asyncio.create_task(
            role_repository.run_verification_role_update_with_identity(
                guild_id,
                706,
                identity=identity,
                expected_rank_last_checked_at=link.rank_last_checked_at,
                operation=apply_roles,
            )
        )
        await asyncio.wait_for(role_apply_started.wait(), timeout=2)
        deletion_task = asyncio.create_task(request_admin_deletion())
        await asyncio.wait_for(deletion_started.wait(), timeout=2)
        completed, _pending = await asyncio.wait({deletion_task}, timeout=0.1)

        assert completed == set()
        assert effects == ["role-apply-started"]

        release_role_apply.set()
        applied = await asyncio.wait_for(apply_task, timeout=2)
        request = await asyncio.wait_for(deletion_task, timeout=2)

        assert applied is True
        assert request.status is VerificationDeletionRequestStatus.REQUESTED
        assert request.link is not None

        blocked_operation = AsyncMock()
        blocked = await role_repository.run_verification_role_update_with_identity(
            guild_id,
            706,
            identity=identity,
            expected_rank_last_checked_at=link.rank_last_checked_at,
            operation=blocked_operation,
        )
        assert not blocked
        blocked_operation.assert_not_awaited()

        async def cleanup_roles() -> bool:
            effects.append("admin-cleanup")
            return True

        claimed_at = request.link.deletion_claimed_at
        assert claimed_at is not None
        deleted = await deletion_repository.process_verification_deletion_with_identity(
            guild_id,
            706,
            identity=identity,
            expected_claimed_at=claimed_at,
            base_delay_seconds=10,
            operation=cleanup_roles,
        )

        assert deleted.status is VerificationDeletionProcessStatus.DELETED
        assert effects == ["role-apply-started", "role-apply-finished", "admin-cleanup"]
    finally:
        release_role_apply.set()
        pending_tasks = [task for task in (apply_task, deletion_task) if task is not None]
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await _delete_verification_link(role_repository, guild_id, 706)
        async with connection.engine.begin() as database_connection:
            await database_connection.execute(
                text("DELETE FROM verification_access_logs WHERE guild_id = :guild_id"),
                {"guild_id": guild_id},
            )
        await connection.close()


async def test_expired_warning_is_atomic_and_does_not_escalate_the_next_warning(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )

    await upgrade_database(settings)
    connection = Database(settings)
    warnings = WarningRepository(connection.session_factory)
    durations = {1: 7, 2: 14, 3: 3}
    try:
        elapsed = await warnings.issue(
            guild_id=guild_id,
            user_id=201,
            requested_level=1,
            reasons="1",
            description=None,
            moderator_id=501,
            message_id=401,
            duration_by_level=durations,
        )
        async with connection.session_factory.begin() as session:
            stored = await session.get(Warning, elapsed.id, with_for_update=True)
            assert stored is not None
            stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        fresh = await warnings.issue(
            guild_id=guild_id,
            user_id=201,
            requested_level=1,
            reasons="2",
            description="fresh",
            moderator_id=502,
            message_id=402,
            duration_by_level=durations,
        )

        assert fresh.level == 1
        assert fresh.parent_id is None
        assert fresh.reasons == "2"
        async with connection.session_factory() as session:
            old = await session.get(Warning, elapsed.id)
            assert old is not None
            assert old.status == WarningStatus.EXPIRED.value
            assert old.role_sync_pending
            assert old.audit_sync_pending
        active = await warnings.get_active(guild_id, 201)
        assert active is not None and active.id == fresh.id

        await warnings.acknowledge_role_sync(guild_id, 201)
        await warnings.acknowledge_audit_sync(guild_id, 402, fresh.id)
        candidates = await warnings.list_for_reconciliation(guild_id)
        assert [item.id for item in candidates] == [elapsed.id, fresh.id]
        async with connection.session_factory() as session:
            stored_fresh = await session.get(Warning, fresh.id)
            assert stored_fresh is not None
            assert not stored_fresh.role_sync_pending
            assert not stored_fresh.audit_sync_pending

        concurrent = await asyncio.gather(
            warnings.issue(
                guild_id=guild_id,
                user_id=203,
                requested_level=1,
                reasons="3",
                description=None,
                moderator_id=503,
                message_id=404,
                duration_by_level=durations,
            ),
            warnings.issue(
                guild_id=guild_id,
                user_id=203,
                requested_level=1,
                reasons="4",
                description=None,
                moderator_id=504,
                message_id=404,
                duration_by_level=durations,
            ),
        )
        assert sorted(item.level for item in concurrent) == [1, 2]
        concurrent_active = await warnings.get_active(guild_id, 203)
        assert concurrent_active is not None and concurrent_active.level == 2

        audit_snapshot = await warnings.issue(
            guild_id=guild_id,
            user_id=204,
            requested_level=1,
            reasons="5",
            description=None,
            moderator_id=505,
            message_id=405,
            duration_by_level=durations,
        )
        newer_audit = await warnings.issue(
            guild_id=guild_id,
            user_id=204,
            requested_level=1,
            reasons="6",
            description=None,
            moderator_id=506,
            message_id=405,
            duration_by_level=durations,
        )
        await warnings.acknowledge_audit_sync(guild_id, 405, audit_snapshot.id)
        async with connection.session_factory() as session:
            stored_snapshot = await session.get(Warning, audit_snapshot.id)
            stored_newer = await session.get(Warning, newer_audit.id)
            assert stored_snapshot is not None and stored_newer is not None
            assert not stored_snapshot.audit_sync_pending
            assert stored_newer.audit_sync_pending
    finally:
        await connection.close()


async def test_expired_timeout_never_reactivates_its_parent(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )

    await upgrade_database(settings)
    connection = Database(settings)
    warnings = WarningRepository(connection.session_factory)
    durations = {1: 7, 2: 14, 3: 3}
    try:
        first = await warnings.issue(
            guild_id=guild_id,
            user_id=202,
            requested_level=1,
            reasons="1",
            description=None,
            moderator_id=501,
            message_id=403,
            duration_by_level=durations,
        )
        second = await warnings.issue(
            guild_id=guild_id,
            user_id=202,
            requested_level=1,
            reasons="2",
            description=None,
            moderator_id=502,
            message_id=403,
            duration_by_level=durations,
        )
        timeout = await warnings.issue(
            guild_id=guild_id,
            user_id=202,
            requested_level=1,
            reasons="3",
            description=None,
            moderator_id=503,
            message_id=403,
            duration_by_level=durations,
        )
        async with connection.session_factory.begin() as session:
            stored = await session.get(Warning, timeout.id, with_for_update=True)
            assert stored is not None
            stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        assert await warnings.get_active(guild_id, 202) is None
        async with connection.session_factory() as session:
            stored_first = await session.get(Warning, first.id)
            stored_second = await session.get(Warning, second.id)
            stored_timeout = await session.get(Warning, timeout.id)
            assert stored_first is not None and stored_second is not None
            assert stored_timeout is not None
            assert stored_first.status == WarningStatus.SUPERSEDED.value
            assert stored_second.status == WarningStatus.SUPERSEDED.value
            assert stored_timeout.status == WarningStatus.EXPIRED.value
    finally:
        await connection.close()


async def test_adaptive_rank_migration_downgrade_preserves_existing_link_data(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    url = make_database_url(settings).render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["guild_id"] = guild_id
    try:
        await upgrade_database(settings, legacy_audit_channel_id=912)
        await asyncio.to_thread(command.downgrade, config, "20260815_0004")
        old_database = Database(settings)
        try:
            async with old_database.engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO verification_links (
                            guild_id, discord_user_id, message_id, platform, puuid,
                            verification_method, last_known_rank, rank_next_refresh_at,
                            rank_refresh_failures, created_at
                        ) VALUES (
                            :guild_id, 909, 808, 'EUN1', :puuid,
                            'PROFILE_ICON', 'EMERALD', NOW(), 0, NOW()
                        )
                        """
                    ),
                    {"guild_id": guild_id, "puuid": f"migration-{guild_id}"},
                )
        finally:
            await old_database.close()

        await asyncio.to_thread(command.upgrade, config, "20260815_0005")
        upgraded = Database(settings)
        try:
            async with upgraded.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT message_id, platform, puuid, last_known_rank,
                                   rank_role_sync_pending, deletion_failures
                            FROM verification_links
                            WHERE guild_id = :guild_id AND discord_user_id = 909
                            """
                        ),
                        {"guild_id": guild_id},
                    )
                ).one()
            assert tuple(row) == (
                808,
                "EUN1",
                f"migration-{guild_id}",
                "EMERALD",
                False,
                0,
            )
        finally:
            await upgraded.close()

        await asyncio.to_thread(command.downgrade, config, "20260815_0004")
        downgraded = Database(settings)
        try:
            async with downgraded.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT message_id, platform, puuid, last_known_rank
                            FROM verification_links
                            WHERE guild_id = :guild_id AND discord_user_id = 909
                            """
                        ),
                        {"guild_id": guild_id},
                    )
                ).one()
            assert tuple(row) == (808, "EUN1", f"migration-{guild_id}", "EMERALD")
        finally:
            await downgraded.close()
    finally:
        await upgrade_database(settings, legacy_audit_channel_id=912)
        cleanup = Database(settings)
        try:
            async with cleanup.engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM verification_links WHERE guild_id = :guild_id"),
                    {"guild_id": guild_id},
                )
        finally:
            await cleanup.close()


async def test_verification_deletion_lifecycle_migration_preserves_existing_links(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
        zweryfikowani_channel_id=912,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    url = make_database_url(settings).render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["guild_id"] = guild_id
    try:
        await upgrade_database(settings, legacy_audit_channel_id=912)
        await asyncio.to_thread(command.downgrade, config, "20260823_0006")
        old_database = Database(settings)
        try:
            async with old_database.engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO verification_links (
                            guild_id, discord_user_id, message_id, platform, puuid,
                            verification_method, rank_next_refresh_at, created_at
                        ) VALUES (
                            :guild_id, 910, 811, 'EUN1', NULL,
                            'LEGACY_MISSING', NOW(), NOW()
                        )
                        """
                    ),
                    {"guild_id": guild_id},
                )
        finally:
            await old_database.close()

        with pytest.raises(RuntimeError, match="legacy_audit_channel_id is required"):
            await asyncio.to_thread(command.upgrade, config, "20260824_0007")
        failed_upgrade = Database(settings)
        try:
            async with failed_upgrade.engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("verification_links")
                    }
                )
            assert "audit_channel_id" not in columns
            assert "deletion_remove_rank_region_roles" not in columns
        finally:
            await failed_upgrade.close()

        config.attributes["legacy_audit_channel_id"] = 912
        await asyncio.to_thread(command.upgrade, config, "20260824_0007")
        upgraded = Database(settings)
        try:
            async with upgraded.engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("verification_links")
                    }
                )
                check_constraints = await connection.run_sync(
                    lambda sync_connection: {
                        constraint["name"]
                        for constraint in inspect(sync_connection).get_check_constraints(
                            "verification_links"
                        )
                    }
                )
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT message_id, puuid, audit_channel_id,
                                   deletion_remove_rank_region_roles
                            FROM verification_links
                            WHERE guild_id = :guild_id AND discord_user_id = 910
                            """
                        ),
                        {"guild_id": guild_id},
                    )
                ).one()
            assert {"audit_channel_id", "deletion_remove_rank_region_roles"} <= columns
            assert "ck_verification_links_audit_message_channel" in check_constraints
            assert tuple(row) == (811, None, 912, False)
        finally:
            await upgraded.close()

        await asyncio.to_thread(command.downgrade, config, "20260823_0006")
        downgraded = Database(settings)
        try:
            async with downgraded.engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync_connection: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns("verification_links")
                    }
                )
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT message_id, platform, puuid, verification_method
                            FROM verification_links
                            WHERE guild_id = :guild_id AND discord_user_id = 910
                            """
                        ),
                        {"guild_id": guild_id},
                    )
                ).one()
            assert "audit_channel_id" not in columns
            assert "deletion_remove_rank_region_roles" not in columns
            assert tuple(row) == (811, "EUN1", None, "LEGACY_MISSING")
        finally:
            await downgraded.close()
    finally:
        config.attributes["legacy_audit_channel_id"] = 912
        await asyncio.to_thread(command.upgrade, config, "head")
        cleanup = Database(settings)
        try:
            async with cleanup.engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM verification_links WHERE guild_id = :guild_id"),
                    {"guild_id": guild_id},
                )
        finally:
            await cleanup.close()


async def _start_rso_web_against_postgres(settings_factory: Callable[..., Settings]):
    from aiohttp.test_utils import TestClient, TestServer

    from moon_poro.rso_web import create_app
    from moon_poro.settings import RSOSettings

    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    database_settings = {
        "postgres_user": os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        "postgres_password": os.getenv(
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),  # pragma: allowlist secret
        "postgres_host": os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        "postgres_port": int(os.environ["TEST_POSTGRES_PORT"]),
        "postgres_db": os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        "guild_id": guild_id,
    }
    await upgrade_database(settings_factory(**database_settings))
    rso_settings = RSOSettings(
        _env_file=None,
        rso_client_id="moon-poro-client",
        rso_client_auth_method="private_key_jwt",
        rso_client_assertion="signed-client-assertion",  # pragma: allowlist secret
        rso_public_base_url="http://localhost:8080",
        **database_settings,
    )
    database = Database(rso_settings)
    client = TestClient(TestServer(create_app(rso_settings, database)))
    await client.start_server()
    return guild_id, database, client


async def _stop_rso_web_against_postgres(guild_id: int, database: Database, client) -> None:
    try:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM verification_links WHERE guild_id=:guild"), {"guild": guild_id}
            )
            await connection.execute(
                text("DELETE FROM verification_sessions WHERE guild_id=:guild"), {"guild": guild_id}
            )
    finally:
        await client.close()


def _oauth_state(response) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]


async def test_rso_double_click_on_postgres_reaches_riot_with_newest_state(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import urlsplit

    import aiohttp

    from moon_poro.rso_web import BROWSER_COOKIE, REPOSITORY_KEY, RSO_CLIENT_KEY, RiotIdentity
    from moon_poro.verification_sessions import hash_secret

    guild_id, database, client = await _start_rso_web_against_postgres(settings_factory)
    try:
        app = client.server.app
        repository = app[REPOSITORY_KEY]
        created = await repository.create(guild_id=guild_id, user_id=456, ttl_seconds=600)
        path = f"/verify/start/{created.token}"
        host = {"Host": "localhost:8080"}
        origin = {**host, "Origin": "http://localhost:8080"}
        landing = await client.get(path, headers=host)
        browser = landing.cookies[BROWSER_COOKIE].value

        # Hold the session row so both clicks queue on the same row lock.
        async with database.engine.connect() as blocker:
            await blocker.begin()
            await blocker.execute(
                text("SELECT id FROM verification_sessions WHERE guild_id=:guild FOR UPDATE"),
                {"guild": guild_id},
            )
            clicks = [
                asyncio.create_task(client.post(path, headers=origin, allow_redirects=False))
                for _click in range(2)
            ]
            await asyncio.sleep(0.2)
            assert not any(click.done() for click in clicks)
            await blocker.rollback()
        responses = await asyncio.wait_for(asyncio.gather(*clicks), timeout=5)

        assert [response.status for response in responses] == [303, 303]
        assert all(
            urlsplit(response.headers["Location"]).netloc == "auth.riotgames.com"
            for response in responses
        )
        states = [_oauth_state(response) for response in responses]
        assert states[0] != states[1]
        stored = await repository.get_by_start_token(created.token)
        assert stored is not None and stored.status == "AWAITING_RIOT"
        assert stored.browser_binding_hash == hash_secret(browser)
        newest = [state for state in states if hash_secret(state) == stored.oauth_state_hash]
        stale = [state for state in states if hash_secret(state) != stored.oauth_state_hash]
        assert len(newest) == len(stale) == 1

        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as foreign:
            rejected = await foreign.post(
                client.make_url(path),
                headers={**origin, "Cookie": f"{BROWSER_COOKIE}={'f' * 43}"},
                allow_redirects=False,
            )
            assert rejected.status == 200
            assert "Logowanie trwa w innej przeglądarce" in await rejected.text()

        monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", AsyncMock(return_value="t"))
        monkeypatch.setattr(
            app[RSO_CLIENT_KEY],
            "get_identity",
            AsyncMock(return_value=RiotIdentity(f"click-{guild_id}", "Moon", "EUNE", "EUN1")),
        )
        old = await client.get(f"/oauth2/callback?state={stale[0]}&code=code", headers=host)
        assert "Ten link logowania jest nieaktualny" in await old.text()
        done = await client.get(
            f"/oauth2/callback?state={newest[0]}&code=code", headers=host, allow_redirects=False
        )
        assert done.status == 303 and done.headers["Location"] == "/verify/result"
        finished = await repository.get_by_start_token(created.token)
        assert finished is not None and finished.status == "VERIFIED_PENDING_DISCORD"
    finally:
        await _stop_rso_web_against_postgres(guild_id, database, client)


async def test_rso_callback_reload_on_postgres_waits_for_first_request(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiohttp

    from moon_poro.rso_web import BROWSER_COOKIE, REPOSITORY_KEY, RSO_CLIENT_KEY, RiotIdentity

    guild_id, database, client = await _start_rso_web_against_postgres(settings_factory)
    try:
        app = client.server.app
        repository = app[REPOSITORY_KEY]
        created = await repository.create(guild_id=guild_id, user_id=456, ttl_seconds=600)
        path = f"/verify/start/{created.token}"
        host = {"Host": "localhost:8080"}
        await client.get(path, headers=host)
        started = await client.post(
            path, headers={**host, "Origin": "http://localhost:8080"}, allow_redirects=False
        )
        callback = f"/oauth2/callback?state={_oauth_state(started)}&code=code"
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_exchange(_code: str) -> str:
            entered.set()
            await release.wait()
            return "temporary-token"

        exchange = AsyncMock(side_effect=slow_exchange)
        monkeypatch.setattr(app[RSO_CLIENT_KEY], "exchange_code", exchange)
        monkeypatch.setattr(
            app[RSO_CLIENT_KEY],
            "get_identity",
            AsyncMock(return_value=RiotIdentity(f"reload-{guild_id}", "Moon", "EUNE", "EUN1")),
        )
        first = asyncio.create_task(client.get(callback, headers=host, allow_redirects=False))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            reloaded = await client.get(callback, headers=host, allow_redirects=False)
            assert reloaded.status == 303
            assert reloaded.headers["Location"] == "/verify/result"
            pending = await client.get("/verify/result", headers=host)
            assert "Potwierdzamy logowanie w Riot" in await pending.text()
            assert pending.headers["Refresh"] == "2"
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as foreign:
                rejected = await foreign.get(
                    client.make_url(callback),
                    headers={**host, "Cookie": f"{BROWSER_COOKIE}={'f' * 43}"},
                    allow_redirects=False,
                )
                assert "Wróć do poprzedniej przeglądarki" in await rejected.text()
        finally:
            release.set()
        response = await asyncio.wait_for(first, timeout=5)

        assert response.status == 303 and response.headers["Location"] == "/verify/result"
        exchange.assert_awaited_once_with("code")
        finished = await repository.get_by_start_token(created.token)
        assert finished is not None and finished.status == "VERIFIED_PENDING_DISCORD"
        page = await client.get("/verify/result", headers=host)
        assert "Riot potwierdził konto" in await page.text()
    finally:
        await _stop_rso_web_against_postgres(guild_id, database, client)


def _launch_fix_settings(
    settings_factory: Callable[..., Settings],
    guild_id: int,
) -> Settings:
    return settings_factory(
        postgres_user=os.getenv("TEST_POSTGRES_USER", "moon_poro_test"),
        postgres_password=os.getenv(  # pragma: allowlist secret
            "TEST_POSTGRES_PASSWORD", "audit-password"
        ),
        postgres_host=os.getenv("TEST_POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ["TEST_POSTGRES_PORT"]),
        postgres_db=os.getenv("TEST_POSTGRES_DB", "moon_poro_test"),
        guild_id=guild_id,
    )


async def test_manual_rank_refresh_jumps_the_backlog_without_spending_cooldown(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = _launch_fix_settings(settings_factory, guild_id)
    await upgrade_database(settings)
    database = Database(settings)
    repository = VerificationRepository(database.session_factory)
    now = datetime.now(UTC)
    try:
        for user_id in (801, 802, 803, 804, 805):
            await repository.create(
                guild_id=guild_id,
                user_id=user_id,
                message_id=None,
                platform="EUN1",
                puuid=f"priority-{guild_id}-{user_id}",
                rank_snapshot=RankSnapshot("GOLD"),
            )
        async with database.session_factory.begin() as session:
            for user_id, hours in ((801, 3), (802, 2), (803, 1)):
                link = await session.get(VerificationLink, (guild_id, user_id))
                assert link is not None
                link.rank_next_refresh_at = now - timedelta(hours=hours)
            failing = await session.get(VerificationLink, (guild_id, 805))
            assert failing is not None
            failing.rank_refresh_failures = 1
            failing.rank_next_refresh_at = now + timedelta(hours=1)
        oldest_before = (await repository.rank_refresh_queue_stats(guild_id)).oldest_due_at
        assert oldest_before is not None

        enqueued = await repository.request_rank_refresh(
            guild_id, 804, cooldown_seconds=1800, source="user"
        )
        assert enqueued.status is RankRefreshRequestStatus.ENQUEUED
        claimed = await repository.claim_due_rank_refreshes(
            guild_id, limit=1, claim_timeout_seconds=300
        )
        assert [link.discord_user_id for link in claimed] == [804]
        again = await repository.request_rank_refresh(
            guild_id, 804, cooldown_seconds=1800, source="user"
        )
        assert again.status is RankRefreshRequestStatus.COOLDOWN

        # 803 is the newest overdue row; a click moves it ahead of 801 and 802
        # without spending the user's cooldown.
        due = await repository.request_rank_refresh(
            guild_id, 803, cooldown_seconds=1800, source="user"
        )
        assert due.status is RankRefreshRequestStatus.ALREADY_DUE
        assert (await repository.get_by_user(guild_id, 803)).rank_user_refresh_requested_at is None
        stats = await repository.rank_refresh_queue_stats(guild_id)
        assert stats.oldest_due_at is not None
        assert oldest_before - stats.oldest_due_at <= timedelta(seconds=1)
        claimed = await repository.claim_due_rank_refreshes(
            guild_id, limit=1, claim_timeout_seconds=300
        )
        assert [link.discord_user_id for link in claimed] == [803]
        running = await repository.request_rank_refresh(
            guild_id, 803, cooldown_seconds=1800, source="user"
        )
        assert running.status is RankRefreshRequestStatus.ALREADY_CLAIMED
        assert (await repository.get_by_user(guild_id, 803)).rank_user_refresh_requested_at is None

        for _attempt in range(2):
            backoff = await repository.request_rank_refresh(
                guild_id, 805, cooldown_seconds=1800, source="user"
            )
            assert backoff.status is RankRefreshRequestStatus.BACKOFF_ACTIVE
        assert (await repository.get_by_user(guild_id, 805)).rank_user_refresh_requested_at is None

        claimed = await repository.claim_due_rank_refreshes(
            guild_id, limit=2, claim_timeout_seconds=300
        )
        assert [link.discord_user_id for link in claimed] == [801, 802]
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM verification_links WHERE guild_id = :guild_id"),
                {"guild_id": guild_id},
            )
        await database.close()


async def test_requested_rank_refreshes_are_served_first_come_first_served(
    settings_factory: Callable[..., Settings],
) -> None:
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = _launch_fix_settings(settings_factory, guild_id)
    await upgrade_database(settings)
    database = Database(settings)
    repository = VerificationRepository(database.session_factory)
    now = datetime.now(UTC)
    try:
        for user_id in (901, 902, 903, 904, 905):
            await repository.create(
                guild_id=guild_id,
                user_id=user_id,
                message_id=None,
                platform="EUN1",
                puuid=f"fifo-{guild_id}-{user_id}",
                rank_snapshot=RankSnapshot("GOLD"),
            )
        async with database.session_factory.begin() as session:
            backlog = await session.get(VerificationLink, (guild_id, 901))
            assert backlog is not None
            backlog.rank_next_refresh_at = now - timedelta(hours=3)
            already_due = await session.get(VerificationLink, (guild_id, 904))
            assert already_due is not None
            already_due.rank_next_refresh_at = now - timedelta(minutes=1)
            deleting = await session.get(VerificationLink, (guild_id, 905))
            assert deleting is not None
            deleting.rank_next_refresh_at = now - timedelta(days=30)
            deleting.deletion_requested_at = now

        for user_id in (902, 903):
            result = await repository.request_rank_refresh(
                guild_id, user_id, cooldown_seconds=1800, source="user"
            )
            assert result.status is RankRefreshRequestStatus.ENQUEUED
        # Clicking again while already due must not jump ahead of earlier requests.
        for _attempt in range(5):
            result = await repository.request_rank_refresh(
                guild_id, 904, cooldown_seconds=1800, source="user"
            )
            assert result.status is RankRefreshRequestStatus.ALREADY_DUE

        claimed = await repository.claim_due_rank_refreshes(
            guild_id, limit=4, claim_timeout_seconds=300
        )
        assert [link.discord_user_id for link in claimed] == [902, 903, 904, 901]
        # A link waiting for deletion never anchors the priority far in the past.
        first = await repository.get_by_user(guild_id, 902)
        assert first is not None
        assert now - first.rank_next_refresh_at < timedelta(hours=4)
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM verification_links WHERE guild_id = :guild_id"),
                {"guild_id": guild_id},
            )
        await database.close()


async def test_stalled_role_operation_releases_the_link_lock_and_keeps_retry_state(
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moon_poro import repositories

    monkeypatch.setattr(repositories, "ROLE_OPERATION_TIMEOUT_SECONDS", 0.2)
    guild_id = time.time_ns() % 9_000_000_000_000_000_000
    settings = _launch_fix_settings(settings_factory, guild_id)
    await upgrade_database(settings)
    database = Database(settings)
    repository = VerificationRepository(database.session_factory)
    try:
        link = await repository.create(
            guild_id=guild_id,
            user_id=811,
            message_id=None,
            platform="EUN1",
            puuid=f"timeout-{guild_id}",
        )
        identity = VerificationLinkIdentity.from_link(link)

        async def stalled_discord_call() -> None:
            await asyncio.sleep(30)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                repository.run_verification_role_update_with_identity(
                    guild_id,
                    811,
                    identity=identity,
                    expected_rank_last_checked_at=None,
                    operation=stalled_discord_call,
                ),
                timeout=5,
            )
        # The row lock is gone: another locking write completes immediately.
        refresh = await asyncio.wait_for(
            repository.request_rank_refresh(guild_id, 811, cooldown_seconds=60, source="user"),
            timeout=2,
        )
        assert refresh.status is RankRefreshRequestStatus.ALREADY_DUE

        request = await repository.request_verification_deletion_with_identity(
            guild_id,
            811,
            identity=identity,
            policy=VerificationDeletionPolicy.USER,
        )
        assert request.link is not None
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                repository.run_verification_deletion_role_cleanup_with_identity(
                    guild_id,
                    811,
                    identity=identity,
                    expected_requested_at=request.link.deletion_requested_at,
                    expected_remove_rank_region_roles=False,
                    operation=stalled_discord_call,
                ),
                timeout=5,
            )

        async def stalled_cleanup() -> bool:
            await asyncio.sleep(30)
            return True

        claimed_at = request.link.deletion_claimed_at
        assert claimed_at is not None
        result = await asyncio.wait_for(
            repository.process_verification_deletion_with_identity(
                guild_id,
                811,
                identity=identity,
                expected_claimed_at=claimed_at,
                base_delay_seconds=30,
                operation=stalled_cleanup,
            ),
            timeout=5,
        )
        assert result.status is VerificationDeletionProcessStatus.RETRY_SCHEDULED
        pending = await repository.get_by_user(guild_id, 811)
        assert pending is not None
        assert pending.deletion_requested_at is not None
        assert pending.deletion_failures == 1
        assert pending.deletion_claimed_at is None
    finally:
        await _delete_verification_link(repository, guild_id, 811)
        await database.close()


async def test_rso_shutdown_finishes_pending_failure_update_on_postgres(
    settings_factory: Callable[..., Settings],
) -> None:
    from moon_poro.rso_web import REPOSITORY_KEY, _start_failure

    guild_id, database, client = await _start_rso_web_against_postgres(settings_factory)
    try:
        repository = client.server.app[REPOSITORY_KEY]
        created = await repository.create(guild_id=guild_id, user_id=456, ttl_seconds=600)
        state, browser = "p" * 64, "b" * 43
        await repository.begin_oauth(token=created.token, state=state, browser_token=browser)
        claimed = await repository.claim_callback(state, browser_token=browser)

        # The failure update queues on the row lock while the app shuts down.
        async with database.engine.connect() as blocker:
            await blocker.begin()
            await blocker.execute(
                text("SELECT id FROM verification_sessions WHERE id=:id FOR UPDATE"),
                {"id": claimed.id},
            )
            task = _start_failure(repository, claimed.id)
            shutdown = asyncio.create_task(client.close())
            await asyncio.sleep(0.3)
            assert not task.done() and not shutdown.done()
            await blocker.rollback()
        await asyncio.wait_for(shutdown, timeout=5)

        assert task.done() and task.exception() is None
        stored = await repository.get_by_start_token(created.token)
        assert stored is not None
        assert (stored.status, stored.error_code) == ("FAILED", "INTERNAL_ERROR")
    finally:
        await _stop_rso_web_against_postgres(guild_id, database, client)
        await database.close()
