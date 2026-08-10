from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moon_poro.cogs import roles
from moon_poro.cogs.roles import RoleCategory, RolesCog, ToggleRoleButton


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = hash(name)


class FakeMember:
    def __init__(self, *, roles_: list[FakeRole] | None = None) -> None:
        self.guild = SimpleNamespace()
        self.roles = roles_ or []
        self.add_roles = AsyncMock()
        self.remove_roles = AsyncMock()


def make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        member_role_name="Member",
        no_lol_role_name="No LoL",
        verified_role_name="Verified",
        lol_servers=["EUNE"],
        lol_ranks=["Gold"],
        lol_positions=["Mid"],
        optional_roles=["TFT"],
        allowed_role_names=frozenset({"EUNE", "Gold", "Mid", "TFT", "Member", "No LoL"}),
        role_ids={},
    )


def make_cog() -> RolesCog:
    cog = object.__new__(RolesCog)
    cog.bot = SimpleNamespace(settings=make_settings())
    return cog


async def test_update_member_role_adds_member_when_requirements_are_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_role = FakeRole("Member")
    member = FakeMember()
    bot = SimpleNamespace(settings=make_settings())
    monkeypatch.setattr(roles, "find_role", lambda *_args: member_role)
    monkeypatch.setattr(roles, "member_roles_named", lambda *_args: [FakeRole("configured")])
    monkeypatch.setattr(roles, "member_has_role", lambda *_args: False)

    await roles._update_member_role(bot, member)

    member.add_roles.assert_awaited_once_with(member_role, reason="Synchronizacja ról Moon Poro")


async def test_update_member_role_removes_orphaned_member_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_role = FakeRole("Member")
    member = FakeMember(roles_=[member_role])
    bot = SimpleNamespace(settings=make_settings())
    monkeypatch.setattr(roles, "find_role", lambda *_args: member_role)
    monkeypatch.setattr(roles, "member_roles_named", lambda *_args: [])
    monkeypatch.setattr(roles, "member_has_role", lambda *_args: False)

    await roles._update_member_role(bot, member)

    member.remove_roles.assert_awaited_once_with(member_role, reason="Synchronizacja ról Moon Poro")


async def test_require_guild_reports_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    safe_send = AsyncMock()
    monkeypatch.setattr(roles, "safe_send", safe_send)
    interaction = SimpleNamespace(guild=None)

    assert await roles._require_guild(interaction) is None
    safe_send.assert_awaited_once()


async def test_toggle_role_button_adds_configured_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(roles.discord, "Member", FakeMember)
    selected = FakeRole("TFT")
    member = FakeMember()
    interaction = SimpleNamespace(
        user=member,
        guild=member.guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    bot = SimpleNamespace(settings=make_settings())
    monkeypatch.setattr(roles, "find_role", lambda *_args: selected)
    monkeypatch.setattr(roles, "member_has_role", lambda *_args: False)
    update_member = AsyncMock()
    monkeypatch.setattr(roles, "_update_member_role", update_member)
    button = ToggleRoleButton(bot, "TFT", RoleCategory.OPTIONAL, row=0)

    await button.callback(interaction)

    member.add_roles.assert_awaited_once_with(selected, reason="Samodzielne nadanie roli")
    update_member.assert_awaited_once_with(bot, member)
    assert "Dodano" in interaction.followup.send.await_args.args[0]


def test_filter_allowed_roles_separates_unconfigured_roles() -> None:
    cog = make_cog()
    allowed = FakeRole("TFT")
    denied = FakeRole("Administrator")

    accepted, rejected = cog._filter_allowed_roles((allowed, denied, None))

    assert accepted == [allowed]
    assert rejected == [denied]


def test_role_problem_detects_multiple_rank_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    cog = make_cog()
    member = FakeMember()

    def configured_roles(_member, names, _settings):
        if names == ["Gold"]:
            return [FakeRole("Gold"), FakeRole("Other rank")]
        if names == ["EUNE"]:
            return [FakeRole("EUNE")]
        return []

    monkeypatch.setattr(roles, "member_roles_named", configured_roles)
    monkeypatch.setattr(roles, "member_has_role", lambda *_args: False)

    assert cog._role_problem(member)


async def test_publish_role_panels_uses_single_shared_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessageable:
        def __init__(self) -> None:
            self.send = AsyncMock()

    monkeypatch.setattr(roles.discord.abc, "Messageable", FakeMessageable)
    cog = make_cog()
    channel = FakeMessageable()
    interaction = SimpleNamespace(
        channel=channel,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await cog._publish_role_panels(interaction, include_all=True)

    interaction.response.send_message.assert_awaited_once()
    assert channel.send.await_count == 4
    assert [call.args[0] for call in channel.send.await_args_list] == [
        "**Region**",
        "**Pozycje**",
        "**Role opcjonalne**",
        "**Pozostałe**",
    ]
