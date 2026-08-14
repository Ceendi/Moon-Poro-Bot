from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.cogs import warnings
from moon_poro.cogs.warnings import (
    WarningRoleUnavailable,
    WarningsCog,
    warning_embed,
)
from moon_poro.models import WarningStatus


class FakeRole:
    def __init__(self, name: str, role_id: int) -> None:
        self.name = name
        self.id = role_id


class FakeMember:
    def __init__(
        self,
        *,
        guild: object,
        user_id: int = 101,
        roles: list[FakeRole] | None = None,
    ) -> None:
        self.bot = False
        self.guild = guild
        self.id = user_id
        self.roles = roles or []
        self.remove_roles = AsyncMock()
        self.add_roles = AsyncMock()


def make_warning(*, warning_id: int = 42, status: str = "ACTIVE") -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=warning_id,
        guild_id=123,
        discord_user_id=101,
        level=1,
        reasons="1/2",
        description="description",
        starts_at=now,
        expires_at=now + timedelta(days=7),
        message_id=301,
        status=status,
        role_sync_pending=True,
        audit_sync_pending=True,
        moderators=[SimpleNamespace(moderator_id=501), SimpleNamespace(moderator_id=502)],
    )


def make_cog() -> WarningsCog:
    cog = object.__new__(WarningsCog)
    repository = SimpleNamespace(
        get_active=AsyncMock(),
        expire_due=AsyncMock(),
        list_for_reconciliation=AsyncMock(return_value=[]),
        acknowledge_role_sync=AsyncMock(),
        acknowledge_audit_sync=AsyncMock(),
        revert=AsyncMock(),
    )
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            warn_channel_id=30,
            warn_roles={1: "Warn", 2: "Warn 2", 3: "TIMEOUT"},
            warn_days={"Warn": 7, "Warn 2": 14, "TIMEOUT": 3},
            role_ids={},
        ),
        warnings=repository,
        get_guild=Mock(return_value=None),
    )
    cog._member_locks = warnings.defaultdict(warnings.asyncio.Lock)
    return cog


def test_warning_embed_contains_readable_audit_states() -> None:
    warning = make_warning()

    active = warning_embed(warning, "Warn")
    expired = warning_embed(warning, "Warn", status=WarningStatus.EXPIRED)
    revoked = warning_embed(warning, "Warn", status=WarningStatus.REVOKED)

    assert active.title == "Warn"
    assert active.footer.text == "ID kary: 42"
    assert {field.name for field in active.fields} >= {"Użytkownik", "Moderatorzy"}
    assert expired.title == "Warn — wygasł"
    assert next(field.value for field in expired.fields if field.name == "Status") == (
        "Wygasł (EXPIRED)"
    )
    assert revoked.title == "Warn — cofnięty"
    assert next(field.value for field in revoked.fields if field.name == "Status") == (
        "Cofnięty (REVOKED)"
    )


def test_duration_mapping_uses_role_configuration() -> None:
    assert make_cog()._duration_by_level() == {1: 7, 2: 14, 3: 3}


async def test_set_warning_role_replaces_other_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog = make_cog()
    guild = SimpleNamespace()
    previous = FakeRole("Warn", 1)
    desired = FakeRole("Warn 2", 2)
    member = FakeMember(guild=guild, roles=[previous])
    monkeypatch.setattr(warnings, "member_roles_named", lambda *_args: [previous])
    monkeypatch.setattr(warnings, "find_role", lambda *_args: desired)

    await cog._set_warning_role(member, 2)

    member.remove_roles.assert_awaited_once_with(previous, reason="Synchronizacja aktywnej kary")
    member.add_roles.assert_awaited_once_with(desired, reason="Synchronizacja aktywnej kary")


async def test_missing_target_role_never_removes_existing_warning_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog = make_cog()
    previous = FakeRole("Warn", 1)
    member = FakeMember(guild=SimpleNamespace(), roles=[previous])
    monkeypatch.setattr(warnings, "member_roles_named", lambda *_args: [previous])
    monkeypatch.setattr(warnings, "find_role", lambda *_args: None)

    with pytest.raises(WarningRoleUnavailable, match="Warn 2"):
        await cog._set_warning_role(member, 2)

    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()


async def test_member_join_restores_logically_active_warning() -> None:
    cog = make_cog()
    warning = SimpleNamespace(level=2)
    cog.bot.warnings.get_active.return_value = warning
    cog._set_warning_role = AsyncMock()
    member = FakeMember(guild=SimpleNamespace(id=123))

    await cog.on_member_join(member)

    cog._set_warning_role.assert_awaited_once_with(member, 2)
    cog.bot.warnings.acknowledge_role_sync.assert_awaited_once_with(123, 101)


async def test_member_update_immediately_restores_manually_removed_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog = make_cog()
    guild = SimpleNamespace(id=123)
    warn_role = FakeRole("Warn", 1)
    before = FakeMember(guild=guild, roles=[warn_role])
    after = FakeMember(guild=guild, roles=[])
    cog.bot.warnings.get_active.return_value = SimpleNamespace(level=1)
    monkeypatch.setattr(warnings, "find_role", lambda *_args: warn_role)

    await cog.on_member_update(before, after)

    after.add_roles.assert_awaited_once_with(warn_role, reason="Synchronizacja aktywnej kary")
    cog.bot.warnings.acknowledge_role_sync.assert_awaited_once_with(123, 101)


async def test_member_update_caused_by_sync_does_not_start_an_edit_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog = make_cog()
    guild = SimpleNamespace(id=123)
    warn_role = FakeRole("Warn", 1)
    before = FakeMember(guild=guild, roles=[])
    after = FakeMember(guild=guild, roles=[warn_role])
    cog.bot.warnings.get_active.return_value = SimpleNamespace(level=1)
    monkeypatch.setattr(warnings, "find_role", lambda *_args: warn_role)

    await cog.on_member_update(before, after)

    after.add_roles.assert_not_awaited()
    after.remove_roles.assert_not_awaited()


async def test_reconciliation_updates_roles_even_without_audit_channel() -> None:
    cog = make_cog()
    guild = SimpleNamespace(
        id=123,
        get_member=Mock(return_value=FakeMember(guild=SimpleNamespace(id=123))),
        get_channel=Mock(return_value=None),
    )
    pending = make_warning(status=WarningStatus.EXPIRED.value)
    cog.bot.get_guild.return_value = guild
    cog.bot.warnings.list_for_reconciliation.return_value = [pending]
    cog.bot.warnings.get_active.return_value = None
    cog._set_warning_role = AsyncMock()

    await WarningsCog.reconcile_warnings.coro(cog)

    cog.bot.warnings.expire_due.assert_awaited_once_with(123)
    cog._set_warning_role.assert_awaited_once()
    cog.bot.warnings.acknowledge_role_sync.assert_awaited_once()
    cog.bot.warnings.acknowledge_audit_sync.assert_not_awaited()


async def test_reconciliation_continues_after_one_member_role_failure() -> None:
    cog = make_cog()
    first = make_warning(warning_id=41)
    second = make_warning(warning_id=42)
    second.discord_user_id = 102
    first.audit_sync_pending = False
    second.audit_sync_pending = False
    guild = SimpleNamespace(
        id=123,
        get_member=Mock(side_effect=lambda user_id: SimpleNamespace(id=user_id)),
        get_channel=Mock(return_value=None),
    )
    cog.bot.get_guild.return_value = guild
    cog.bot.warnings.list_for_reconciliation.return_value = [first, second]
    cog._sync_member_warning_role = AsyncMock(side_effect=[WarningRoleUnavailable("missing"), None])

    await WarningsCog.reconcile_warnings.coro(cog)

    assert cog._sync_member_warning_role.await_count == 2


async def test_reconciliation_retries_failed_audit_without_blocking_role_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessageable:
        pass

    monkeypatch.setattr(warnings.discord.abc, "Messageable", FakeMessageable)
    cog = make_cog()
    pending = make_warning(status=WarningStatus.EXPIRED.value)
    member = SimpleNamespace(id=101)
    channel = FakeMessageable()
    guild = SimpleNamespace(
        id=123,
        get_member=Mock(return_value=member),
        get_channel=Mock(return_value=channel),
    )
    cog.bot.get_guild.return_value = guild
    cog.bot.warnings.list_for_reconciliation.return_value = [pending]
    cog._sync_member_warning_role = AsyncMock()
    cog._sync_audit_message = AsyncMock(side_effect=RuntimeError("Discord unavailable"))

    await WarningsCog.reconcile_warnings.coro(cog)

    cog._sync_member_warning_role.assert_awaited_once_with(member)
    cog._sync_audit_message.assert_awaited_once_with(channel, pending)
    cog.bot.warnings.acknowledge_audit_sync.assert_not_awaited()


async def test_cw_edits_audit_to_revoked_instead_of_deleting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessageable:
        pass

    monkeypatch.setattr(warnings.discord.abc, "Messageable", FakeMessageable)
    cog = make_cog()
    current = make_warning(status=WarningStatus.REVOKED.value)
    cog.bot.warnings.revert.return_value = (current, None)
    cog._sync_audit_message = AsyncMock()
    cog._sync_member_warning_role = AsyncMock()
    channel = FakeMessageable()
    guild = SimpleNamespace(id=123, get_channel=Mock(return_value=channel))
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    member = SimpleNamespace(id=101)

    await WarningsCog.revert_warning.callback(cog, interaction, member)

    cog._sync_audit_message.assert_awaited_once_with(channel, current)
    cog._sync_member_warning_role.assert_awaited_once_with(member)
    assert "Cofnięto aktywną karę" in interaction.followup.send.await_args.args[0]


async def test_cw_role_sync_survives_audit_edit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessageable:
        pass

    monkeypatch.setattr(warnings.discord.abc, "Messageable", FakeMessageable)
    cog = make_cog()
    current = make_warning(status=WarningStatus.REVOKED.value)
    cog.bot.warnings.revert.return_value = (current, None)
    cog._sync_audit_message = AsyncMock(side_effect=RuntimeError("Discord unavailable"))
    cog._sync_member_warning_role = AsyncMock()
    channel = FakeMessageable()
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=123, get_channel=Mock(return_value=channel)),
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    member = SimpleNamespace(id=101)

    await WarningsCog.revert_warning.callback(cog, interaction, member)

    cog._sync_member_warning_role.assert_awaited_once_with(member)
    assert "automatycznie ponowiona" in interaction.followup.send.await_args.args[0]
