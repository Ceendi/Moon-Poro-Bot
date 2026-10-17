import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from moon_poro.cogs.verification import (
    LeagueEntries,
    VerificationCog,
    _apply_verified_roles,
    _process_requested_verification_deletion,
    _reconcile_applied_roles,
    _reconcile_cached_role_change,
    _record_leagues,
    _refresh_next_verified,
    _remove_verified_marker_durably,
    _run_active_link_role_update,
)
from moon_poro.member_role_lock import member_role_lock
from moon_poro.models import Base, VerificationMarkerCleanup
from moon_poro.repositories import (
    VerificationDeletionPolicy,
    VerificationLinkIdentity,
    VerificationRepository,
    VerificationSnapshotChanged,
)
from moon_poro.riot import RiotAuthBreaker


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[VerificationRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield VerificationRepository(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class Role:
    id: int
    name: str


class Member:
    def __init__(self, guild: SimpleNamespace, roles: list[Role]) -> None:
        self.id = 456
        self.guild = guild
        self.roles = list(roles)

    async def add_roles(self, *roles: Role, reason: str) -> None:
        self.guild.actual.update(roles)
        self.guild.added.extend(roles)

    async def remove_roles(self, *roles: Role, reason: str) -> None:
        self.guild.actual.difference_update(roles)


def role_context(repository: VerificationRepository) -> tuple[SimpleNamespace, SimpleNamespace]:
    gold, emerald, eune, verified, member_role = roles = [
        Role(1, "Gold"),
        Role(2, "Emerald"),
        Role(3, "EUNE"),
        Role(4, "Verified"),
        Role(5, "Member"),
    ]
    guild = SimpleNamespace(id=123, roles=roles, actual={eune, verified, member_role}, added=[])

    async def fetch_member(_user_id: int) -> Member:
        return Member(guild, list(guild.actual))

    guild.fetch_member = AsyncMock(side_effect=fetch_member)
    bot = SimpleNamespace(
        verifications=repository,
        settings=SimpleNamespace(
            guild_id=123,
            lol_ranks=[gold.name, emerald.name],
            lol_servers=[eune.name],
            role_ids={},
            verified_role_name=verified.name,
            member_role_name=member_role.name,
            rank_refresh_policy="adaptive",
            rank_refresh_rollout_percent=100,
            rank_refresh_interval_hours=24,
            rank_refresh_retry_base_seconds=300,
        ),
    )
    return bot, guild


async def store_emerald(bot: SimpleNamespace) -> tuple[LeagueEntries, datetime]:
    link = await bot.verifications.get_by_user(123, 456)
    assert link is not None
    recorded = await _record_leagues(
        bot,
        link,
        [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}],
        expected_claimed_at=None,
    )
    assert recorded is not None
    return recorded


async def test_stale_snapshot_never_runs_discord_after_new_snapshot_was_acknowledged(
    repository: VerificationRepository,
) -> None:
    await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
        rank_tier="GOLD",
    )
    observed = await repository.get_by_user(123, 456)
    assert observed is not None
    bot, guild = role_context(repository)
    leagues, checked_at = await store_emerald(bot)
    member = Member(guild, list(guild.actual))
    assert await _run_active_link_role_update(
        bot,
        observed,
        lambda: _apply_verified_roles(bot, member, "EUN1", leagues),
        expected_rank_last_checked_at=checked_at,
    )
    assert await repository.acknowledge_rank_role_sync(
        123,
        456,
        expected_rank_last_checked_at=checked_at,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=observed.created_at,
    )
    stale_operation = AsyncMock()
    with pytest.raises(VerificationSnapshotChanged):
        await repository.run_verification_role_update_with_identity(
            123,
            456,
            identity=VerificationLinkIdentity.from_link(observed),
            expected_rank_last_checked_at=observed.rank_last_checked_at,
            operation=stale_operation,
        )
    stale_operation.assert_not_awaited()
    current = await repository.get_by_user(123, 456)
    assert current is not None and current.last_known_rank == "EMERALD"
    assert current.rank_role_sync_pending is False
    assert {role.name for role in guild.actual} == {"Emerald", "EUNE", "Verified", "Member"}


async def test_member_event_rereads_snapshot_after_worker_applied_and_acknowledged_newer_rank(
    repository: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
        rank_tier="GOLD",
    )
    bot, guild = role_context(repository)
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    original_get = repository.get_by_user
    intercepted = False

    async def delayed_read(guild_id: int, user_id: int):
        nonlocal intercepted
        link = await original_get(guild_id, user_id)
        if not intercepted:
            intercepted = True
            leagues, checked_at = await store_emerald(bot)
            assert link is not None
            await _run_active_link_role_update(
                bot,
                link,
                lambda: cog.apply_verified_roles(
                    Member(guild, list(guild.actual)), "EUN1", leagues
                ),
                expected_rank_last_checked_at=checked_at,
            )
            assert await repository.acknowledge_rank_role_sync(
                guild_id,
                user_id,
                expected_rank_last_checked_at=checked_at,
                expected_puuid=link.puuid,
                expected_platform=link.platform,
                expected_created_at=link.created_at,
            )
        return link

    monkeypatch.setattr(repository, "get_by_user", delayed_read)
    before = Member(guild, [*guild.actual, guild.roles[0]])
    after = Member(guild, list(guild.actual))
    await _reconcile_cached_role_change(cog, before, after)
    current = await original_get(123, 456)
    assert current is not None and current.last_known_rank == "EMERALD"
    assert current.rank_role_sync_pending is False
    assert "Gold" not in {role.name for role in guild.added}
    assert {role.name for role in guild.actual} == {"Emerald", "EUNE", "Verified", "Member"}


async def test_rso_snapshot_conflict_retries_without_failing_valid_link(
    repository: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
        rank_tier="GOLD",
        method="RSO",
    )
    link = await repository.get_by_user(123, 456)
    assert link is not None
    bot, guild = role_context(repository)
    guild.get_member = Mock(return_value=Member(guild, list(guild.actual)))
    bot.get_guild = Mock(return_value=guild)
    bot.verification_sessions = SimpleNamespace(retry_discord=AsyncMock(), fail_discord=AsyncMock())
    original_guard = repository.run_verification_role_update_with_identity

    async def superseded_guard(*args: object, **kwargs: object):
        await store_emerald(bot)
        return await original_guard(*args, **kwargs)

    monkeypatch.setattr(repository, "run_verification_role_update_with_identity", superseded_guard)
    record = SimpleNamespace(
        id=1,
        guild_id=123,
        discord_user_id=456,
        platform="EUN1",
        puuid="puuid",
        created_at=link.rank_last_checked_at,
        completion_attempts=1,
    )
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    cog._publish_rso_audit = AsyncMock()
    await cog._complete_rso_verification(record)
    bot.verification_sessions.retry_discord.assert_awaited_once_with(
        1,
        error_code="RANK_SNAPSHOT_CONFLICT",
        delay_seconds=5,
        max_attempts=8,
    )
    bot.verification_sessions.fail_discord.assert_not_awaited()
    cog._publish_rso_audit.assert_not_awaited()
    current = await repository.get_by_user(123, 456)
    assert current is not None and current.puuid == "puuid" and current.last_known_rank == "EMERALD"


async def test_no_snapshot_ack_cannot_clear_newer_snapshot_intent(
    repository: VerificationRepository,
) -> None:
    await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
    )
    observed = await repository.get_by_user(123, 456)
    assert observed is not None and observed.rank_last_checked_at is None
    bot, _guild = role_context(repository)
    await store_emerald(bot)
    assert not await repository.acknowledge_rank_role_sync(
        123,
        456,
        expected_rank_last_checked_at=None,
        expected_puuid=observed.puuid,
        expected_platform=observed.platform,
        expected_created_at=observed.created_at,
    )
    current = await repository.get_by_user(123, 456)
    assert current is not None and current.rank_role_sync_pending is True


async def test_refresh_worker_uses_new_recorded_version_for_roles_and_ack(
    repository: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
    )
    bot, guild = role_context(repository)
    bot.settings.rank_refresh_claim_timeout_seconds = 300
    bot.riot_auth_breaker = RiotAuthBreaker()
    bot.get_guild = Mock(return_value=guild)
    guild.get_member = Mock(return_value=Member(guild, list(guild.actual)))
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    # Extension registration tests unload/re-import the module. Patch the
    # globals of this imported function, not a potentially newer module object.
    monkeypatch.setitem(
        _refresh_next_verified.__globals__,
        "_get_leagues",
        AsyncMock(return_value=[{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]),
    )
    monkeypatch.setitem(
        _refresh_next_verified.__globals__, "_sync_riot_id", AsyncMock(return_value=True)
    )
    await _refresh_next_verified(cog)
    current = await repository.get_by_user(123, 456)
    assert current is not None and current.last_known_rank == "EMERALD"
    assert current.rank_role_sync_pending is False
    assert {role.name for role in guild.actual} == {"Emerald", "EUNE", "Verified", "Member"}


@pytest.mark.parametrize("cancel_while_waiting", [False, True])
async def test_deletion_takes_member_lock_before_repository_and_can_retry_after_cancellation(
    repository: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
    cancel_while_waiting: bool,
) -> None:
    created = await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
        rank_tier="GOLD",
    )
    requested = await repository.request_verification_deletion_with_identity(
        123,
        456,
        identity=VerificationLinkIdentity.from_link(created),
        policy=VerificationDeletionPolicy.USER,
    )
    assert requested.link is not None
    bot, guild = role_context(repository)
    guild.actual.add(guild.roles[0])
    session_cancelled = asyncio.Event()

    async def cancel_sessions(*_args: object, **_kwargs: object) -> None:
        session_cancelled.set()

    bot.verification_sessions = SimpleNamespace(
        cancel_for_user=AsyncMock(side_effect=cancel_sessions)
    )
    process = AsyncMock(wraps=repository.process_verification_deletion_with_identity)
    monkeypatch.setattr(repository, "process_verification_deletion_with_identity", process)

    async def delete() -> bool:
        return await _process_requested_verification_deletion(
            bot,
            guild=guild,
            member=Member(guild, list(guild.actual)),
            link=requested.link,
        )

    async with member_role_lock(123, 456):
        deletion = asyncio.create_task(delete())
        await asyncio.wait_for(session_cancelled.wait(), timeout=2)
        # Session cancellation has completed, but no link row lock may be taken
        # while this task is waiting for the panel's member lock.
        process.assert_not_awaited()
        if cancel_while_waiting:
            deletion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await deletion
            assert await repository.get_by_user(123, 456) is not None
    if cancel_while_waiting:
        assert await delete()
    else:
        assert await asyncio.wait_for(deletion, timeout=2)
    process.assert_awaited_once()
    assert await repository.get_by_user(123, 456) is None
    assert {role.name for role in guild.actual} == {"Gold", "EUNE", "Member"}


class DiscordRoleState:
    """Real members keep their role snapshots unchanged after HTTP mutations."""

    def __init__(self, repository: VerificationRepository, *initial: str) -> None:
        client = discord.Client(intents=discord.Intents.none())
        self.state = client._connection
        names = ("@everyone", "Gold", "EUNE", "Verified", "Member", "No LoL")
        self.guild = discord.Guild(
            state=self.state,
            data={
                "id": "123",
                "name": "Test",
                "owner_id": "999",
                "roles": [
                    {"id": str(i + 123), "name": name, "position": i}
                    for i, name in enumerate(names)
                ],
            },
        )
        self.roles = {role.name: role for role in self.guild.roles}
        self.actual = {self.roles[name].id for name in initial}
        self.http = SimpleNamespace(
            add_role=AsyncMock(side_effect=self.add),
            remove_role=AsyncMock(side_effect=self.remove),
            get_member=AsyncMock(side_effect=self.fetch),
        )
        self.state.http = self.http
        self.member = discord.Member(data=self.payload(), guild=self.guild, state=self.state)
        self.bot, _guild = role_context(repository)
        self.bot.settings.no_lol_role_name = "No LoL"
        self.bot.verification_sessions = SimpleNamespace(cancel_for_user=AsyncMock())

    def payload(self) -> dict:
        return {
            "user": {"id": "456", "username": "test", "discriminator": "0", "avatar": None},
            "roles": [str(role_id) for role_id in self.actual],
            "flags": 0,
        }

    async def fetch(self, *_args: object) -> dict:
        return self.payload()

    async def add(self, _guild: int, _user: int, role_id: int, **_kwargs: object) -> None:
        self.actual.add(role_id)

    async def remove(self, _guild: int, _user: int, role_id: int, **_kwargs: object) -> None:
        self.actual.discard(role_id)

    def selected(self) -> set[str]:
        return {name for name, role in self.roles.items() if role.id in self.actual}

    def cog(self) -> VerificationCog:
        cog = object.__new__(VerificationCog)
        cog.bot = self.bot
        cog._managed_role_updates = set()
        return cog


async def test_delete_fetches_roles_added_after_interaction_member_snapshot(
    repository: VerificationRepository,
) -> None:
    env = DiscordRoleState(repository, "Gold", "EUNE", "Member")
    created = await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
        rank_tier="GOLD",
    )
    # Discord accepted a concurrent completion's role addition; the interaction
    # object still contains its original roles, just like discord.py in production.
    await env.member.add_roles(env.roles["Verified"])
    assert "Verified" not in {role.name for role in env.member.roles}
    requested = await repository.request_verification_deletion_with_identity(
        123,
        456,
        identity=VerificationLinkIdentity.from_link(created),
        policy=VerificationDeletionPolicy.USER,
    )
    assert requested.link is not None
    assert await _process_requested_verification_deletion(
        env.bot,
        guild=env.guild,
        member=env.member,
        link=requested.link,
    )
    assert await repository.get_by_user(123, 456) is None
    assert env.selected() == {"Gold", "EUNE", "Member"}
    env.http.get_member.assert_awaited_once_with(123, 456)


async def test_marker_compensation_fetches_roles_added_after_original_member_snapshot(
    repository: VerificationRepository,
) -> None:
    env = DiscordRoleState(repository, "Gold", "EUNE", "Member")
    old_link = await repository.create(
        guild_id=123,
        user_id=456,
        message_id=None,
        platform="EUN1",
        puuid="puuid",
        rank_tier="GOLD",
    )
    await env.member.add_roles(env.roles["Verified"])
    async with repository._sessions.begin() as session:
        await session.delete(await session.get(type(old_link), (123, 456)))
    assert not await _reconcile_applied_roles(env.cog(), env.member, old_link)
    assert env.selected() == {"Gold", "EUNE", "Member"}
    assert "Verified" not in {role.name for role in env.member.roles}
    async with repository._sessions() as session:
        assert await session.get(VerificationMarkerCleanup, (123, 456)) is None


async def test_marker_cleanup_rechecks_replacement_after_waiting_for_member_lock(
    repository: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = DiscordRoleState(repository, "Gold", "EUNE", "Member")
    enqueued = asyncio.Event()
    original_enqueue = repository.enqueue_verified_marker_cleanup

    async def enqueue(*args: int) -> int:
        generation = await original_enqueue(*args)
        enqueued.set()
        return generation

    monkeypatch.setattr(repository, "enqueue_verified_marker_cleanup", enqueue)
    async with member_role_lock(123, 456):
        cleanup = asyncio.create_task(
            _remove_verified_marker_durably(env.cog(), env.member, reason="old completion")
        )
        await asyncio.wait_for(enqueued.wait(), timeout=2)
        await repository.create(
            guild_id=123,
            user_id=456,
            message_id=None,
            platform="EUN1",
            puuid="replacement",
            rank_tier="GOLD",
        )
        await env.member.add_roles(env.roles["Verified"])
    await asyncio.wait_for(cleanup, timeout=2)
    env.http.remove_role.assert_not_awaited()
    assert env.selected() == {"Gold", "EUNE", "Member", "Verified"}


async def test_unknown_rank_delayed_bot_gateway_events_converge_without_role_oscillation(
    repository: VerificationRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await repository.create(
        guild_id=123, user_id=456, message_id=None, platform="EUN1", puuid="puuid"
    )
    bot, guild = role_context(repository)
    bot.settings.rank_refresh_manual_priority_cooldown_seconds = 3600
    request_refresh = AsyncMock()
    monkeypatch.setattr(repository, "request_rank_refresh", request_refresh)
    bot.settings.no_lol_role_name = "No LoL"
    iron = Role(6, "Iron")
    no_lol = Role(7, "No LoL")
    unranked = Role(8, "Unranked")
    guild.roles.extend([iron, no_lol, unranked])
    bot.settings.lol_ranks.extend([iron.name, unranked.name])
    guild.actual = {iron, no_lol}
    queued_events: list[tuple[Member, Member]] = []

    class EventMember(Member):
        async def remove_roles(self, *roles: Role, reason: str) -> None:
            before = Member(guild, list(guild.actual))
            await super().remove_roles(*roles, reason=reason)
            queued_events.append((before, Member(guild, list(guild.actual))))

        async def add_roles(self, *roles: Role, reason: str) -> None:
            before = Member(guild, list(guild.actual))
            await super().add_roles(*roles, reason=reason)
            queued_events.append((before, Member(guild, list(guild.actual))))

    async def fetch_member(_user_id: int) -> Member:
        return EventMember(guild, list(guild.actual))

    guild.fetch_member = AsyncMock(side_effect=fetch_member)
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    await _reconcile_cached_role_change(cog, Member(guild, []), Member(guild, [iron, no_lol]))
    assert cog._managed_role_updates == set()
    assert len(queued_events) == 2
    # Deliver HTTP-generated Gateway events after the transient managed flag was
    # cleared, preserving their obsolete snapshots just as Discord does.
    for before, after in list(queued_events):
        await _reconcile_cached_role_change(cog, before, after)
    assert len(queued_events) == 2
    assert {role.name for role in guild.actual} == {"EUNE", "Verified", "Member"}
    assert not {iron, unranked} & set(guild.added)
    current = await repository.get_by_user(123, 456)
    assert current is not None and current.last_known_rank is None
    assert request_refresh.await_count == 3
    request_refresh.assert_awaited_with(123, 456, cooldown_seconds=3600, source="role_tamper")
    async with repository._sessions() as session:
        assert await session.get(VerificationMarkerCleanup, (123, 456)) is None


async def test_verification_replaces_no_lol_role_using_fresh_member(
    repository: VerificationRepository,
) -> None:
    env = DiscordRoleState(repository, "No LoL")
    async with member_role_lock(123, 456):
        await _apply_verified_roles(
            env.bot,
            env.member,
            "EUN1",
            [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}],
        )
    assert env.selected() == {"Gold", "EUNE", "Member", "Verified"}
    assert "No LoL" in {role.name for role in env.member.roles}


@pytest.mark.parametrize("cleanup", ["deletion", "marker"])
async def test_failed_fresh_member_fetch_keeps_cleanup_durable(
    repository: VerificationRepository,
    cleanup: str,
) -> None:
    env = DiscordRoleState(repository, "Gold", "EUNE", "Member", "Verified")
    env.http.get_member.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "member fetch unavailable"
    )
    if cleanup == "deletion":
        created = await repository.create(
            guild_id=123,
            user_id=456,
            message_id=None,
            platform="EUN1",
            puuid="puuid",
            rank_tier="GOLD",
        )
        requested = await repository.request_verification_deletion_with_identity(
            123,
            456,
            identity=VerificationLinkIdentity.from_link(created),
            policy=VerificationDeletionPolicy.USER,
        )
        assert requested.link is not None
        assert not await _process_requested_verification_deletion(
            env.bot,
            guild=env.guild,
            member=env.member,
            link=requested.link,
        )
        pending = await repository.get_by_user(123, 456)
        assert pending is not None and pending.deletion_requested_at is not None
        assert pending.deletion_failures == 1
    else:
        await _remove_verified_marker_durably(env.cog(), env.member, reason="old completion")
        async with repository._sessions() as session:
            pending_marker = await session.get(VerificationMarkerCleanup, (123, 456))
            assert pending_marker is not None and pending_marker.failures == 1
    env.http.remove_role.assert_not_awaited()
    assert env.selected() == {"Gold", "EUNE", "Member", "Verified"}
