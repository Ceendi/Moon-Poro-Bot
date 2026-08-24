from types import SimpleNamespace
from unittest.mock import Mock

from moon_poro.cogs.roles import MiscRolesView, RankDropdown
from moon_poro.roles import find_role, member_has_role, member_roles_named, role_is_configured


async def test_role_controls_use_short_user_facing_labels() -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(lol_ranks=["Iron IV"]))

    dropdown = RankDropdown(bot)
    misc = MiscRolesView(bot)
    labels = {item.custom_id: item.label for item in misc.children}

    assert dropdown.placeholder == "Wybierz rangę Solo/Duo"
    assert labels == {
        "roles:no-lol:v2": "Nie mam konta w LoL",
        "roles:remove-all:v2": "Usuń wszystkie wybrane role",
    }


def test_find_role_prefers_stable_configured_id() -> None:
    renamed = SimpleNamespace(id=10, name="Renamed")
    same_name = SimpleNamespace(id=11, name="Expected")
    guild = SimpleNamespace(roles=[renamed, same_name], get_role=Mock(return_value=renamed))
    settings = SimpleNamespace(role_ids={"Expected": 10})

    assert find_role(guild, "Expected", settings) is renamed


def test_find_role_falls_back_to_name_without_configured_id() -> None:
    expected = SimpleNamespace(id=10, name="Expected")
    guild = SimpleNamespace(roles=[expected])
    settings = SimpleNamespace(role_ids={})

    assert find_role(guild, "Expected", settings) is expected
    assert find_role(guild, "Missing", settings) is None


def test_member_roles_named_supports_ids_and_fallback_names() -> None:
    by_id = SimpleNamespace(id=10, name="Renamed")
    by_name = SimpleNamespace(id=11, name="Optional")
    ignored = SimpleNamespace(id=12, name="Ignored")
    member = SimpleNamespace(roles=[by_id, by_name, ignored])
    settings = SimpleNamespace(role_ids={"Rank": 10})

    assert member_roles_named(member, {"Rank", "Optional"}, settings) == [by_id, by_name]


def test_role_configuration_and_membership_use_same_binding_rules() -> None:
    configured = SimpleNamespace(id=10, name="Renamed")
    fallback = SimpleNamespace(id=11, name="Fallback")
    member = SimpleNamespace(roles=[configured, fallback])
    settings = SimpleNamespace(role_ids={"Configured": 10})

    assert role_is_configured(configured, "Configured", settings)
    assert not role_is_configured(fallback, "Configured", settings)
    assert member_has_role(member, "Configured", settings)
    assert member_has_role(member, "Fallback", settings)
    assert not member_has_role(member, "Missing", settings)
