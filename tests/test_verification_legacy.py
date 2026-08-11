from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from moon_poro.cogs import verification_legacy
from moon_poro.cogs.verification_legacy import (
    LegacyVerificationCog,
    _normalize_platform,
    _remove_verified_marker,
)


class FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name


def test_normalize_platform_accepts_supported_aliases() -> None:
    assert _normalize_platform("eune") == "EUN1"
    assert _normalize_platform("EUW1") == "EUW1"
    assert _normalize_platform("na") == "NA1"
    assert _normalize_platform("BR") is None


async def test_remove_own_verification_marker_keeps_rank_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = FakeRole(1, "Zweryfikowany")
    rank = FakeRole(2, "Emerald")
    region = FakeRole(3, "EUNE")
    member = SimpleNamespace(
        guild=object(),
        roles=[verified, rank, region],
        remove_roles=AsyncMock(),
    )
    bot = SimpleNamespace(settings=SimpleNamespace(verified_role_name="Zweryfikowany"))
    monkeypatch.setattr(verification_legacy, "find_role", lambda *_args: verified)

    await _remove_verified_marker(bot, member, reason="test")

    member.remove_roles.assert_awaited_once_with(verified, reason="test")


async def test_manual_rank_change_is_reconciled_from_riot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    verified = FakeRole(3, "Zweryfikowany")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald, verified])
    after = SimpleNamespace(id=101, guild=guild, roles=[emerald, iron, verified])
    link = SimpleNamespace(platform="EUN1", puuid="puuid")
    settings = SimpleNamespace(
        guild_id=123,
        lol_ranks=["Iron", "Emerald"],
        lol_servers=["EUNE"],
        verified_role_name="Zweryfikowany",
        member_role_name="Użytkownik",
        role_ids={},
    )
    bot = SimpleNamespace(
        settings=settings,
        verifications=SimpleNamespace(get_by_user=AsyncMock(return_value=link)),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]
    monkeypatch.setattr(verification_legacy, "_get_leagues", AsyncMock(return_value=leagues))

    await cog.on_member_update(before, after)

    bot.verifications.get_by_user.assert_awaited_once_with(123, 101)
    cog.apply_verified_roles.assert_awaited_once_with(after, "EUN1", leagues)
