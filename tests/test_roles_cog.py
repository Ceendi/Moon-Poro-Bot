from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from moon_poro.cogs import roles
from moon_poro.cogs.roles import (
    MiscRolesView,
    RankDropdown,
    RankSelect,
    RoleButtons,
    RoleCategory,
    RolesCog,
    ToggleRoleButton,
)


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = hash(name)


class FakeMember:
    def __init__(self, *, roles_: list[FakeRole] | None = None) -> None:
        self.id = 42
        self.guild = SimpleNamespace(id=7, fetch_member=AsyncMock(return_value=self))
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


async def test_update_member_role_never_removes_member_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_role = FakeRole("Member")
    member = FakeMember(roles_=[member_role])
    bot = SimpleNamespace(settings=make_settings())
    monkeypatch.setattr(roles, "find_role", lambda *_args: member_role)
    monkeypatch.setattr(roles, "member_roles_named", lambda *_args: [])
    monkeypatch.setattr(roles, "member_has_role", lambda *_args: False)

    assert not await roles._update_member_role(bot, member)

    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()


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
    update_member.assert_awaited_once()
    assert update_member.await_args.args[1].member is member
    assert selected in update_member.await_args.args[1].roles
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


GUILD_ROLES = {
    name: FakeRole(name)
    for name in ("Member", "No LoL", "Verified", "EUNE", "Gold", "Silver", "Mid", "TFT")
}


def make_real_settings() -> SimpleNamespace:
    settings = make_settings()
    settings.lol_ranks = ["Gold", "Silver"]
    return settings


def use_guild_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(roles, "find_role", lambda _guild, name, _settings: GUILD_ROLES.get(name))


def make_interaction(member: FakeMember) -> SimpleNamespace:
    done = [False]

    async def defer(**_kwargs: object) -> None:
        done[0] = True

    return SimpleNamespace(
        user=member,
        guild=member.guild,
        response=SimpleNamespace(
            defer=AsyncMock(side_effect=defer),
            send_message=AsyncMock(),
            is_done=lambda: done[0],
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


@pytest.mark.parametrize(
    "names",
    [
        ["Member"],
        ["Member", "EUNE"],
        ["Member", "Gold"],
        ["Member", "Verified", "EUNE"],
    ],
)
async def test_member_role_without_region_or_rank_is_kept_everywhere(
    monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    use_guild_roles(monkeypatch)
    cog = make_cog()
    cog.bot.settings = make_real_settings()
    member = FakeMember(roles_=[GUILD_ROLES[name] for name in names])

    assert not cog._role_problem(member)
    assert not await roles._update_member_role(cog.bot, member)
    member.remove_roles.assert_not_awaited()


def test_role_problem_flags_qualified_member_without_member_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_guild_roles(monkeypatch)
    cog = make_cog()
    cog.bot.settings = make_real_settings()

    assert cog._role_problem(FakeMember(roles_=[GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"]]))
    assert cog._role_problem(FakeMember(roles_=[GUILD_ROLES["No LoL"]]))


async def test_repair_roles_adds_missing_member_role_and_keeps_existing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_guild_roles(monkeypatch)
    cog = make_cog()
    cog.bot.settings = make_real_settings()
    kept = FakeMember(roles_=[GUILD_ROLES["Member"]])
    qualified = FakeMember(roles_=[GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"]])
    qualified.id = 43
    qualified.guild = SimpleNamespace(id=7, fetch_member=AsyncMock(return_value=qualified))
    guild = SimpleNamespace(members=[kept, qualified])
    interaction = SimpleNamespace(
        guild=guild,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await RolesCog.repair_roles.callback(cog, interaction)

    kept.remove_roles.assert_not_awaited()
    kept.add_roles.assert_not_awaited()
    kept.guild.fetch_member.assert_not_awaited()
    qualified.add_roles.assert_awaited_once_with(
        GUILD_ROLES["Member"], reason="Synchronizacja ról Moon Poro"
    )
    assert "1" in interaction.followup.send.await_args.args[0]


async def test_toggling_region_off_keeps_member_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(roles.discord, "Member", FakeMember)
    use_guild_roles(monkeypatch)
    member = FakeMember(roles_=[GUILD_ROLES["Member"], GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"]])
    bot = SimpleNamespace(settings=make_real_settings())
    button = ToggleRoleButton(bot, "EUNE", RoleCategory.SERVER, row=0)

    await button.callback(make_interaction(member))

    member.remove_roles.assert_awaited_once_with(
        GUILD_ROLES["EUNE"], reason="Samodzielne usunięcie roli"
    )


async def test_remove_all_keeps_member_role_for_unverified_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(roles.discord, "Member", FakeMember)
    use_guild_roles(monkeypatch)
    member = FakeMember(
        roles_=[GUILD_ROLES["Member"], GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"], GUILD_ROLES["TFT"]]
    )
    view = MiscRolesView(SimpleNamespace(settings=make_real_settings()))

    await view.remove_all.callback(make_interaction(member))

    removed = member.remove_roles.await_args.args
    assert GUILD_ROLES["Member"] not in removed
    assert set(removed) == {GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"], GUILD_ROLES["TFT"]}


async def test_rank_swap_adds_new_rank_before_removing_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(roles.discord, "Member", FakeMember)
    use_guild_roles(monkeypatch)
    calls: list[tuple[str, tuple[FakeRole, ...]]] = []
    member = FakeMember(roles_=[GUILD_ROLES["Member"], GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"]])
    member.add_roles = AsyncMock(side_effect=lambda *r, reason: calls.append(("add", r)))
    member.remove_roles = AsyncMock(side_effect=lambda *r, reason: calls.append(("remove", r)))
    dropdown = RankDropdown(SimpleNamespace(settings=make_real_settings()))
    dropdown._values = ["Silver"]
    interaction = make_interaction(member)

    await dropdown.callback(interaction)

    assert calls == [("add", (GUILD_ROLES["Silver"],)), ("remove", (GUILD_ROLES["Gold"],))]
    assert "Silver" in interaction.followup.send.await_args.args[0]


async def test_failed_rank_add_keeps_old_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(roles.discord, "Member", FakeMember)
    use_guild_roles(monkeypatch)
    member = FakeMember(roles_=[GUILD_ROLES["Member"], GUILD_ROLES["EUNE"], GUILD_ROLES["Gold"]])
    member.add_roles = AsyncMock(side_effect=forbidden())
    dropdown = RankDropdown(SimpleNamespace(settings=make_real_settings()))
    dropdown._values = ["Silver"]

    with pytest.raises(discord.Forbidden):
        await dropdown.callback(make_interaction(member))

    member.remove_roles.assert_not_awaited()


async def test_failed_old_rank_removal_rolls_back_new_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_guild_roles(monkeypatch)
    member = FakeMember(roles_=[GUILD_ROLES["Gold"]])
    member.remove_roles = AsyncMock(side_effect=[forbidden(), None])
    state = roles._RoleState(member)

    with pytest.raises(discord.Forbidden):
        await roles._swap_rank_role(state, GUILD_ROLES["Silver"], make_real_settings())

    assert [call.args for call in member.remove_roles.await_args_list] == [
        (GUILD_ROLES["Gold"],),
        (GUILD_ROLES["Silver"],),
    ]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (forbidden(), roles.ROLE_PANEL_FORBIDDEN_MESSAGE),
        (RuntimeError("database is down"), roles.ROLE_PANEL_ERROR_MESSAGE),
    ],
)
@pytest.mark.parametrize("view_factory", ["rank", "buttons", "misc"])
async def test_role_panel_errors_answer_the_deferred_interaction(
    error: Exception, expected: str, view_factory: str
) -> None:
    bot = SimpleNamespace(settings=make_real_settings())
    view = {
        "rank": lambda: RankSelect(bot),
        "buttons": lambda: RoleButtons(bot, ["TFT"], RoleCategory.OPTIONAL),
        "misc": lambda: MiscRolesView(bot),
    }[view_factory]()
    interaction = make_interaction(FakeMember())
    await interaction.response.defer(ephemeral=True)

    await view.on_error(interaction, error, view.children[0])

    interaction.followup.send.assert_awaited_once_with(expected, ephemeral=True)
