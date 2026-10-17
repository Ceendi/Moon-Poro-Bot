from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moon_poro.cogs import verification
from moon_poro.cogs.verification import VerificationCog
from moon_poro.cogs.verification_legacy import LegacyVerificationCog
from moon_poro.models import Base, VerificationLink, VerificationSession
from moon_poro.verification_sessions import VerificationSessionRepository


@pytest_asyncio.fixture
async def audit_repository() -> AsyncIterator[VerificationSessionRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = VerificationSessionRepository(async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield repository
    finally:
        await engine.dispose()


async def pending_session(repository: VerificationSessionRepository) -> VerificationSession:
    created = await repository.create(guild_id=123, user_id=456, ttl_seconds=600)
    await repository.begin_oauth(token=created.token, state="state", browser_token="browser")
    record = await repository.claim_callback("state", browser_token="browser")
    await repository.reserve_link(
        session_id=record.id, platform="EUN1", puuid="puuid", game_name="Moon", tag_line="EUNE"
    )
    return (await repository.claim_pending())[0]


def audit_cog(repository: VerificationSessionRepository, channel: Mock) -> VerificationCog:
    cog = object.__new__(VerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(zweryfikowani_channel_id=654),
        user=SimpleNamespace(id=789),
        is_ready=Mock(return_value=True),
        get_channel=Mock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
        verification_sessions=repository,
    )
    return cog


def audit_channel(messages: list[SimpleNamespace]) -> Mock:
    async def history(**_kwargs: object) -> AsyncIterator[SimpleNamespace]:
        for message in messages:
            yield message

    channel = Mock(spec=discord.TextChannel)
    channel.guild = SimpleNamespace(me=SimpleNamespace(id=789), fetch_member=AsyncMock())
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, read_message_history=True
    )
    channel.history = Mock(side_effect=history)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=987))
    return channel


async def test_audit_failure_is_durable_and_retry_recovers_without_duplicate(
    audit_repository: VerificationSessionRepository,
) -> None:
    record = await pending_session(audit_repository)
    messages: list[SimpleNamespace] = []
    channel = audit_channel(messages)

    async def sent_before_crash(**kwargs: object) -> None:
        messages.append(
            SimpleNamespace(id=987, author=SimpleNamespace(id=789), embeds=[kwargs["embed"]])
        )
        raise RuntimeError("simulated failure after Discord accepted the message")

    channel.send.side_effect = sent_before_crash
    cog = audit_cog(audit_repository, channel)
    with pytest.raises(RuntimeError, match="simulated failure"):
        await cog._publish_rso_audit(record)
    async with audit_repository._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None and saved.audit_pending_channel_id == 654
        assert saved.status == "APPLYING_DISCORD"
    await audit_repository.retry_discord(record.id, error_code="AUDIT_FAILED", delay_seconds=0)
    restarted = VerificationSessionRepository(audit_repository._sessions)
    retried = (await restarted.claim_pending())[0]
    cog.bot.verification_sessions = restarted
    # Config changes must not move the retry to a new channel.
    cog.bot.settings.zweryfikowani_channel_id = 111
    assert await cog._publish_rso_audit(retried)
    assert channel.send.await_count == 1
    assert channel.send.await_args.kwargs["nonce"] == f"rso:{record.id}"
    cog.bot.get_channel.assert_called_with(654)
    async with restarted._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        link = await session.get(VerificationLink, (123, 456))
        assert saved is not None and saved.status == "COMPLETED"
        assert saved.audit_pending_channel_id is None
        assert link is not None and link.message_id == 987 and link.audit_channel_id == 654


async def test_history_failure_never_sends_a_new_message(
    audit_repository: VerificationSessionRepository,
) -> None:
    record = await pending_session(audit_repository)
    channel = audit_channel([])

    async def broken_history(**_kwargs: object) -> AsyncIterator[SimpleNamespace]:
        raise RuntimeError("history unavailable")
        yield  # pragma: no cover

    channel.history.side_effect = broken_history
    with pytest.raises(RuntimeError, match="history unavailable"):
        await audit_cog(audit_repository, channel)._publish_rso_audit(record)
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_duplicate_audit_reconciliation_and_cancel_cleanup(
    audit_repository: VerificationSessionRepository,
    cancelled: bool,
) -> None:
    record = await pending_session(audit_repository)
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.audit_pending_channel_id = 654
    marker = discord.Embed()
    marker.set_footer(text=f"Weryfikacja RSO #{record.id}")
    messages = [
        SimpleNamespace(id=987, author=SimpleNamespace(id=789), embeds=[marker]),
        SimpleNamespace(id=988, author=SimpleNamespace(id=789), embeds=[marker]),
        # An identical footer from somebody else is not our audit event.
        SimpleNamespace(id=989, author=SimpleNamespace(id=1), embeds=[marker]),
    ]
    if cancelled:
        await audit_repository.cancel_for_user(123, 456, created_at_or_before=record.created_at)
        _, purged = await audit_repository.expire_and_purge(retention_days=0)
        assert purged == 0  # Intent must survive until orphan reconciliation.
        records = await audit_repository.claim_audit_reconciliations()
        assert [item.id for item in records] == [record.id]
    channel = audit_channel(messages)
    assert await audit_cog(audit_repository, channel)._publish_rso_audit(record) is not cancelled
    channel.send.assert_not_awaited()
    cleanups = await audit_repository.claim_audit_cleanups(limit=10, claim_timeout_seconds=300)
    assert {item.message_id for item in cleanups} == ({987, 988} if cancelled else {988})
    async with audit_repository._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None and saved.audit_pending_channel_id is None


async def test_missing_cached_channel_is_fetched(
    audit_repository: VerificationSessionRepository,
) -> None:
    record = await pending_session(audit_repository)
    channel = audit_channel([])
    cog = audit_cog(audit_repository, channel)
    cog.bot.get_channel.return_value = None
    assert await cog._publish_rso_audit(record)
    cog.bot.fetch_channel.assert_awaited_once_with(654)
    channel.send.assert_awaited_once()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("failure", ["channel_fetch", "history"])
@pytest.mark.parametrize("error_type", [discord.NotFound, discord.Forbidden])
async def test_missing_audit_destination_only_finishes_inactive_not_found_intents(
    audit_repository: VerificationSessionRepository,
    cancelled: bool,
    failure: str,
    error_type: type[discord.HTTPException],
) -> None:
    record = await pending_session(audit_repository)
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.audit_pending_channel_id = 654
    if cancelled:
        await audit_repository.cancel_for_user(123, 456, created_at_or_before=record.created_at)
    channel = audit_channel([])
    cog = audit_cog(audit_repository, channel)
    error = error_type(
        SimpleNamespace(status=404 if error_type is discord.NotFound else 403, reason="test"),
        "audit destination unavailable",
    )
    if failure == "channel_fetch":
        cog.bot.get_channel.return_value = None
        cog.bot.fetch_channel.side_effect = error
    else:

        async def broken_history(**_kwargs: object) -> AsyncIterator[SimpleNamespace]:
            raise error
            yield  # pragma: no cover

        channel.history.side_effect = broken_history
    cleaned = cancelled and error_type is discord.NotFound
    if cleaned:
        assert not await cog._publish_rso_audit(record)
        assert await audit_repository.claim_audit_reconciliations() == []
    else:
        with pytest.raises(error_type):
            await cog._publish_rso_audit(record)
    channel.send.assert_not_awaited()
    assert await audit_repository.claim_audit_cleanups(limit=10, claim_timeout_seconds=300) == []
    async with audit_repository._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        assert saved.audit_pending_channel_id == (None if cleaned else 654)
        assert saved.status == ("CANCELLED" if cancelled else "APPLYING_DISCORD")


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("permission", ["view_channel", "read_message_history"])
async def test_missing_history_permissions_keep_intent_without_send_or_cleanup(
    audit_repository: VerificationSessionRepository,
    cancelled: bool,
    permission: str,
) -> None:
    record = await pending_session(audit_repository)
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.audit_pending_channel_id = 654
    if cancelled:
        await audit_repository.cancel_for_user(123, 456, created_at_or_before=record.created_at)
    channel = audit_channel([])
    permissions = channel.permissions_for.return_value
    setattr(permissions, permission, False)
    with pytest.raises(RuntimeError, match="history is not readable"):
        await audit_cog(audit_repository, channel)._publish_rso_audit(record)
    channel.history.assert_not_called()
    channel.send.assert_not_awaited()
    assert await audit_repository.claim_audit_cleanups(limit=10, claim_timeout_seconds=300) == []
    async with audit_repository._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None and saved.audit_pending_channel_id == 654
    # Reconciliation remains possible after permissions are restored, including
    # terminal sessions whose existing audit must be removed.
    setattr(permissions, permission, True)
    marker = discord.Embed()
    marker.set_footer(text=f"Weryfikacja RSO #{record.id}")
    channel.history.side_effect = audit_channel(
        [SimpleNamespace(id=987, author=SimpleNamespace(id=789), embeds=[marker])]
    ).history.side_effect
    assert await audit_cog(audit_repository, channel)._publish_rso_audit(record) is not cancelled
    channel.send.assert_not_awaited()
    cleanups = await audit_repository.claim_audit_cleanups(limit=10, claim_timeout_seconds=300)
    assert [item.message_id for item in cleanups] == ([987] if cancelled else [])


@pytest.mark.parametrize("failure", ["not_ready", "member_fetch", "channel_fetch"])
async def test_audit_access_readiness_failures_preserve_terminal_intent(
    audit_repository: VerificationSessionRepository,
    failure: str,
) -> None:
    record = await pending_session(audit_repository)
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.audit_pending_channel_id = 654
    await audit_repository.cancel_for_user(123, 456, created_at_or_before=record.created_at)
    channel = audit_channel([])
    cog = audit_cog(audit_repository, channel)
    if failure == "not_ready":
        cog.bot.is_ready.return_value = False
    elif failure == "member_fetch":
        channel.guild.me = None
        channel.guild.fetch_member.side_effect = RuntimeError("member unavailable")
    else:
        cog.bot.get_channel.return_value = None
        cog.bot.fetch_channel.side_effect = RuntimeError("channel unavailable")
    with pytest.raises(RuntimeError):
        await cog._publish_rso_audit(record)
    channel.history.assert_not_called()
    channel.send.assert_not_awaited()
    async with audit_repository._sessions() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None and saved.audit_pending_channel_id == 654
    assert await audit_repository.claim_audit_cleanups(limit=10, claim_timeout_seconds=300) == []


async def test_orphan_audit_cleanup_fetches_channel_when_guild_is_not_cached(
    audit_repository: VerificationSessionRepository,
) -> None:
    record = await pending_session(audit_repository)
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.audit_pending_channel_id = 654
    await audit_repository.cancel_for_user(123, 456, created_at_or_before=record.created_at)
    marker = discord.Embed()
    marker.set_footer(text=f"Weryfikacja RSO #{record.id}")
    channel = audit_channel(
        [SimpleNamespace(id=987, author=SimpleNamespace(id=789), embeds=[marker])]
    )
    cog = audit_cog(audit_repository, channel)
    assert not await cog._publish_rso_audit(record)
    cog.bot.settings.rank_refresh_claim_timeout_seconds = 300
    cog.bot.settings.rank_refresh_retry_base_seconds = 300
    cog.bot.get_guild = Mock(return_value=None)
    delete = AsyncMock()
    channel.get_partial_message.return_value = SimpleNamespace(delete=delete)
    await verification._retry_next_rso_audit_cleanup(cog)
    cog.bot.fetch_channel.assert_awaited_once_with(654)
    delete.assert_awaited_once()
    assert await audit_repository.claim_audit_cleanups(limit=10, claim_timeout_seconds=300) == []


async def test_cancelled_audit_is_recovered_after_rollback_to_legacy(
    audit_repository: VerificationSessionRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await pending_session(audit_repository)
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.audit_pending_channel_id = 654
    await audit_repository.cancel_for_user(123, 456, created_at_or_before=record.created_at)
    embed = discord.Embed()
    embed.set_footer(text=f"Weryfikacja RSO #{record.id}")
    channel = audit_channel(
        [
            SimpleNamespace(id=987, author=SimpleNamespace(id=789), embeds=[embed]),
        ]
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = audit_cog(audit_repository, channel).bot
    audit_worker = LegacyVerificationCog.reconcile_verification_audits.coro
    monkeypatch.setitem(audit_worker.__globals__, "_retry_next_rso_audit_cleanup", AsyncMock())
    await audit_worker(cog)
    channel.send.assert_not_awaited()
    cleanups = await audit_repository.claim_audit_cleanups(limit=5, claim_timeout_seconds=300)
    assert [item.message_id for item in cleanups] == [987]


async def test_rso_worker_finishes_accepted_icon_and_rso_sessions() -> None:
    records = [
        SimpleNamespace(id=1, verification_method="RSO"),
        SimpleNamespace(id=2, verification_method="PROFILE_ICON"),
    ]
    cog = object.__new__(VerificationCog)
    cog.bot = SimpleNamespace(
        verification_sessions=SimpleNamespace(claim_pending=AsyncMock(return_value=records))
    )
    cog._complete_rso_verification = AsyncMock()
    await VerificationCog.complete_rso_verifications.coro(cog)
    cog.bot.verification_sessions.claim_pending.assert_awaited_once_with(limit=5, method=None)
    assert [call.args[0] for call in cog._complete_rso_verification.await_args_list] == records


async def test_icon_audit_nonce_fits_discord_limit_for_largest_bigint_session(
    audit_repository: VerificationSessionRepository,
) -> None:
    created = await audit_repository.create(
        guild_id=123, user_id=456, ttl_seconds=600, method="PROFILE_ICON"
    )
    record = await audit_repository.get_by_start_token(created.token)
    assert record is not None
    largest_bigint = 9_223_372_036_854_775_807
    async with audit_repository._sessions.begin() as session:
        saved = await session.get(VerificationSession, record.id)
        assert saved is not None
        saved.id = largest_bigint
    await audit_repository.reserve_link(
        session_id=largest_bigint, platform="EUN1", puuid="puuid", game_name="Moon", tag_line="EUNE"
    )
    record = (await audit_repository.claim_pending(method="PROFILE_ICON"))[0]
    channel = audit_channel([])
    assert await audit_cog(audit_repository, channel)._publish_rso_audit(record)
    nonce = channel.send.await_args.kwargs["nonce"]
    assert nonce == f"icon:{largest_bigint}"
    assert len(nonce) <= 25
