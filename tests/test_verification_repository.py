from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moon_poro.models import Base, VerificationLink
from moon_poro.rank_refresh import RankSnapshot
from moon_poro.repositories import (
    RankRefreshRequestStatus,
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


async def test_create_caches_optional_riot_id_without_breaking_legacy_callers(
    verification_repository: VerificationRepository,
) -> None:
    cached = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
        platform="EUN1",
        puuid="cached-puuid",
        riot_game_name="Moon Poro",
        riot_tag_line="EUNE",
    )
    legacy = await verification_repository.create(
        guild_id=123,
        user_id=102,
        message_id=202,
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
        platform="EUN1",
        puuid="same-puuid",
        rank_snapshot=RankSnapshot("GOLD"),
    )
    await verification_repository.delete_by_user(123, 101)
    # Windows' wall clock can advance in ~16 ms ticks. Cross at least one tick so
    # this exercises the created_at generation guard even on SQLite.
    await asyncio.sleep(0.05)
    replacement = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=202,
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
    deletion = await verification_repository.request_verification_deletion(
        123,
        101,
        expected_puuid=old.puuid,
        expected_platform=old.platform,
        expected_created_at=stale_created_at,
    )
    current = await verification_repository.get_by_user(123, 101)

    assert refresh.status == RankRefreshRequestStatus.LINK_CHANGED
    assert deletion is None
    assert current is not None
    assert current.created_at == replacement.created_at
    assert current.rank_user_refresh_requested_at is None
    assert current.deletion_requested_at is None


async def test_matching_identity_and_omitted_identity_preserve_existing_semantics(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
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
    deletion = await verification_repository.request_verification_deletion(123, 101)
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
    assert deletion is not None
    assert deletion.deletion_requested_at is not None
    assert blocked_refresh.status == RankRefreshRequestStatus.NOT_LINKED


async def test_rank_refresh_request_returns_snapshot_baseline_from_locked_link(
    verification_repository: VerificationRepository,
) -> None:
    link = await verification_repository.create(
        guild_id=123,
        user_id=101,
        message_id=201,
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
