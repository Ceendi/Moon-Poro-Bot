from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from moon_poro.cogs.verification_legacy import LegacyVerificationCog
from moon_poro.models import Base, VerificationLink, VerificationSession
from moon_poro.verification_sessions import VerificationSessionRepository


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[VerificationSessionRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield VerificationSessionRepository(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure", ["process", "db_commit"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_legacy_audit_recovery_after_send_and_restart(
    repository: VerificationSessionRepository, failure: str, cancelled: bool
) -> None:
    created = await repository.create(
        guild_id=123, user_id=456, ttl_seconds=120, method="PROFILE_ICON"
    )
    record = await repository.get_by_start_token(created.token)
    assert record is not None
    await repository.reserve_link(
        session_id=record.id, platform="EUN1", puuid="puuid", game_name="Moon", tag_line="EUNE"
    )
    record = (await repository.claim_pending(method="PROFILE_ICON"))[0]
    messages = []
    fail_commit = False

    def reject_commit(_session: Session) -> None:
        if fail_commit:
            raise OperationalError("COMMIT", {}, RuntimeError("simulated database failure"))

    async def history(**_kwargs: object) -> AsyncIterator[SimpleNamespace]:
        for message in messages:
            yield message

    async def send(**kwargs: object) -> SimpleNamespace:
        nonlocal fail_commit
        message = SimpleNamespace(id=987, author=SimpleNamespace(id=789), embeds=[kwargs["embed"]])
        messages.append(message)
        if failure == "process":
            raise RuntimeError("simulated process crash after accepted send")
        fail_commit = True
        return message

    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(me=SimpleNamespace(id=789), fetch_member=AsyncMock())
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, read_message_history=True
    )
    channel.history = Mock(side_effect=history)
    channel.send = AsyncMock(side_effect=send)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(zweryfikowani_channel_id=654),
        user=SimpleNamespace(id=789),
        is_ready=Mock(return_value=True),
        get_channel=Mock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
        verification_sessions=repository,
    )
    event.listen(Session, "before_commit", reject_commit)
    try:
        with pytest.raises((RuntimeError, OperationalError)):
            await cog._publish_rso_audit(record)
    finally:
        event.remove(Session, "before_commit", reject_commit)
    async with repository._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        link = await session.get(VerificationLink, (123, 456))
        assert saved is not None and saved.audit_pending_channel_id == 654
        assert link is not None and link.message_id is None

    restarted = VerificationSessionRepository(repository._sessions)
    cog.bot.verification_sessions = restarted
    if cancelled:
        await restarted.cancel_for_user(123, 456, created_at_or_before=record.created_at)
        async with restarted._sessions.begin() as session:
            link = await session.get(VerificationLink, (123, 456))
            assert link is not None
            await session.delete(link)
        retried = (await restarted.claim_audit_reconciliations())[0]
    else:
        # A restarted worker recovers an abandoned claim after its lease expires.
        async with restarted._sessions.begin() as session:
            saved = await session.get(VerificationSession, record.id)
            assert saved is not None
            saved.updated_at = datetime.now(UTC) - timedelta(minutes=6)
        assert await restarted.claim_pending() == []
        retried = (await restarted.claim_pending(method="PROFILE_ICON"))[0]
    assert await cog._publish_rso_audit(retried) is not cancelled
    assert channel.send.await_count == 1
    async with restarted._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        link = await session.get(VerificationLink, (123, 456))
        assert saved is not None and saved.audit_pending_channel_id is None
        if cancelled:
            assert link is None
        else:
            assert saved.status == "COMPLETED"
            assert link is not None and link.message_id == 987
    cleanups = await restarted.claim_audit_cleanups(limit=5, claim_timeout_seconds=300)
    assert [item.message_id for item in cleanups] == ([987] if cancelled else [])


async def test_legacy_worker_only_claims_icon_sessions() -> None:
    cog = object.__new__(LegacyVerificationCog)
    record = SimpleNamespace(id=1)
    cog.bot = SimpleNamespace(
        verification_sessions=SimpleNamespace(claim_pending=AsyncMock(return_value=[record]))
    )
    cog._complete_rso_verification = AsyncMock()
    await LegacyVerificationCog.complete_legacy_verifications.coro(cog)
    cog.bot.verification_sessions.claim_pending.assert_awaited_once_with(
        limit=5, method="PROFILE_ICON"
    )
    cog._complete_rso_verification.assert_awaited_once_with(record)
