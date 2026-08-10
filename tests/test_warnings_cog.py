from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moon_poro.cogs import warnings
from moon_poro.cogs.warnings import WarningsCog, warning_embed


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


def make_cog() -> WarningsCog:
    cog = object.__new__(WarningsCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(
            warn_roles={1: "Warn", 2: "Warn 2", 3: "TIMEOUT"},
            warn_days={"Warn": 7, "Warn 2": 14, "TIMEOUT": 3},
        ),
        warnings=SimpleNamespace(get_active=AsyncMock()),
    )
    return cog


def test_warning_embed_contains_audit_fields() -> None:
    now = datetime.now(UTC)
    warning = SimpleNamespace(
        id=42,
        reasons="1/2",
        description="description",
        starts_at=now,
        expires_at=now + timedelta(days=7),
        discord_user_id=101,
        moderators=[SimpleNamespace(moderator_id=501), SimpleNamespace(moderator_id=502)],
    )

    embed = warning_embed(warning, "Warn")
    expired = warning_embed(warning, "Warn", expired=True)

    assert embed.title == "Warn"
    assert embed.footer.text == "ID kary: 42"
    assert {field.name for field in embed.fields} >= {"Użytkownik", "Moderatorzy"}
    assert expired.title == "Warn — wygasł"


def test_duration_mapping_uses_role_configuration() -> None:
    assert make_cog()._duration_by_level() == {1: 7, 2: 14, 3: 3}


async def test_set_warning_role_replaces_other_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog = make_cog()
    previous = FakeRole("Warn")
    desired = FakeRole("Warn 2")
    member = SimpleNamespace(
        guild=object(),
        roles=[previous],
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    monkeypatch.setattr(warnings, "member_roles_named", lambda *_args: [previous])
    monkeypatch.setattr(warnings, "find_role", lambda *_args: desired)

    await cog._set_warning_role(member, 2)

    member.remove_roles.assert_awaited_once_with(previous, reason="Synchronizacja aktywnej kary")
    member.add_roles.assert_awaited_once_with(desired, reason="Synchronizacja aktywnej kary")


async def test_member_join_restores_active_warning() -> None:
    cog = make_cog()
    warning = SimpleNamespace(level=2)
    cog.bot.warnings.get_active.return_value = warning
    cog._set_warning_role = AsyncMock()
    member = SimpleNamespace(guild=SimpleNamespace(id=123), id=101)

    await cog.on_member_join(member)

    cog._set_warning_role.assert_awaited_once_with(member, 2)
