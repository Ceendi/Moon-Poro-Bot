from types import SimpleNamespace

import pytest

from moon_poro import permissions


class FakeMember:
    def __init__(
        self,
        *,
        administrator: bool = False,
        moderate_members: bool = False,
        manage_messages: bool = False,
    ) -> None:
        self.guild_permissions = SimpleNamespace(
            administrator=administrator,
            moderate_members=moderate_members,
            manage_messages=manage_messages,
        )


def test_non_member_has_no_guild_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permissions.discord, "Member", FakeMember)
    interaction = SimpleNamespace(user=object())

    assert not permissions.is_administrator(interaction)
    assert not permissions.is_moderator(interaction)


@pytest.mark.parametrize(
    ("permission", "expected"),
    [
        ("administrator", True),
        ("moderate_members", True),
        ("manage_messages", True),
        (None, False),
    ],
)
def test_moderator_permissions(
    monkeypatch: pytest.MonkeyPatch, permission: str | None, expected: bool
) -> None:
    monkeypatch.setattr(permissions.discord, "Member", FakeMember)
    flags = {permission: True} if permission else {}
    interaction = SimpleNamespace(user=FakeMember(**flags))

    assert permissions.is_moderator(interaction) is expected
    assert permissions.is_administrator(interaction) is (permission == "administrator")
