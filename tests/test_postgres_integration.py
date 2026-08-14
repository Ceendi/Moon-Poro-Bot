from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect

from moon_poro.database import Database, upgrade_database
from moon_poro.models import Base, Warning, WarningStatus
from moon_poro.repositories import (
    GuildFeatureRepository,
    ModerationStatsRepository,
    VerificationRepository,
    WarningRepository,
)
from moon_poro.settings import Settings
from moon_poro.verification_sessions import (
    LinkReservationResult,
    VerificationSessionRepository,
)

pytestmark = pytest.mark.skipif(
    "TEST_POSTGRES_PORT" not in os.environ,
    reason="requires an isolated PostgreSQL test database",
)


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
        assert set(Base.metadata.tables) <= table_names
        assert {"role_sync_pending", "audit_sync_pending"} <= warning_columns
        assert "ix_warnings_pending_sync" in warning_indexes

        verifications = VerificationRepository(connection.session_factory)
        created = await verifications.create(
            guild_id=guild_id,
            user_id=101,
            message_id=201,
            platform="EUN1",
            puuid="integration-puuid",
        )
        assert await verifications.get_by_user(guild_id, 101) is not None
        assert (await verifications.get_by_puuid(guild_id, "integration-puuid")).message_id == 201
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
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 1
        assert (
            await verifications.retry_rank_refresh(
                guild_id,
                101,
                base_delay_seconds=300,
            )
            == 300
        )
        assert (await verifications.rank_refresh_queue_stats(guild_id)).due_count == 0
        assert (
            await verifications.claim_due_rank_refreshes(
                guild_id,
                limit=1,
                claim_timeout_seconds=300,
            )
            == []
        )
        assert await verifications.schedule_rank_refresh_now(guild_id, 101)
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
        await rso_sessions.complete_discord(pending[0].id, message_id=202)
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

        stats = await ModerationStatsRepository(connection.session_factory).list_for_guild(guild_id)
        assert {(row.moderator_id, row.warnings_count) for row in stats} == {(501, 1), (502, 1)}

        removed = await verifications.delete_by_user(guild_id, 101)
        assert removed is not None and removed.puuid == "integration-puuid"
        assert await verifications.get_by_user(guild_id, 101) is None
        await verifications.delete_by_user(guild_id, 102)
    finally:
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
        await warnings.acknowledge_audit_sync(guild_id, 402)
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
