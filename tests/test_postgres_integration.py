from __future__ import annotations

import os
import time
from collections.abc import Callable

import pytest
from sqlalchemy import inspect

from moon_poro.database import Database, upgrade_database
from moon_poro.models import Base
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
        assert set(Base.metadata.tables) <= table_names

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
