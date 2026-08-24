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
    WarningRepository,
)
from moon_poro.settings import Settings
from moon_poro.verification_sessions import (
    LinkReservationResult,
    VerificationSessionRepository,
)

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
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 1
        retry_delay = await verifications.retry_rank_refresh(
            guild_id,
            101,
            base_delay_seconds=300,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
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
        assert await verifications.defer_rank_refresh(
            guild_id,
            101,
            delay_seconds=0,
            expected_puuid=created.puuid or "",
            expected_platform=created.platform,
            expected_created_at=created.created_at,
        )
        assert [
            link.discord_user_id
            for link in await verifications.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )
        ] == [101]
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 1
        assert await verifications.complete_rank_refresh(
            guild_id,
            101,
            rank_tier="EMERALD",
            refresh_interval_hours=24,
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
        await rso_sessions.begin_oauth(token=rso.token, state=oauth_state)
        callback = await rso_sessions.claim_callback(oauth_state)
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
                **identity,
            )
            is None
        )
        assert not await repository.release_rank_refresh_claim(guild_id, 703, **identity)
        assert not await repository.defer_rank_refresh(
            guild_id,
            703,
            delay_seconds=7 * 86_400,
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
        assert request.link.deletion_remove_rank_region_roles
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
