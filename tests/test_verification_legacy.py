from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


async def test_legacy_riot_lookups_normalize_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_lookup = AsyncMock(return_value={"puuid": "account-puuid"})
    summoner_lookup = AsyncMock(return_value={"profileIconId": 7})
    bot = SimpleNamespace(
        riot_client=SimpleNamespace(
            get_account_v1_by_riot_id=account_lookup,
            get_lol_summoner_v4_by_puuid=summoner_lookup,
        )
    )

    async def execute(call: Callable[[], Awaitable[object]], *, not_found: object) -> object:
        assert not_found is None
        return await call()

    monkeypatch.setattr(verification_legacy, "riot_api_call", execute)

    account = await verification_legacy._get_account(bot, " Moon Poro ", " #EUNE ", "EUN1")
    summoner = await verification_legacy._get_summoner(bot, "EUN1", "account-puuid")

    assert account == {"puuid": "account-puuid"}
    assert summoner == {"profileIconId": 7}
    account_lookup.assert_awaited_once_with(
        game_name="Moon Poro",
        tag_line="EUNE",
        region=verification_legacy.API_SERVERS["EUN1"],
    )
    summoner_lookup.assert_awaited_once_with(region="EUN1", puuid="account-puuid")


async def test_apply_verified_roles_marks_internal_role_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    bot = SimpleNamespace()
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    apply_roles = AsyncMock()
    monkeypatch.setattr(verification_legacy, "_apply_verified_roles", apply_roles)
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]

    await cog.apply_verified_roles(member, "EUN1", leagues)

    apply_roles.assert_awaited_once_with(bot, member, "EUN1", leagues)
    assert cog._managed_role_updates == set()


async def test_member_join_restores_verified_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(platform="EUN1", puuid="account-puuid")
    bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=SimpleNamespace(get_by_user=AsyncMock(return_value=link)),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]
    monkeypatch.setattr(verification_legacy, "_get_leagues", AsyncMock(return_value=leagues))

    await cog.on_member_join(member)

    bot.verifications.get_by_user.assert_awaited_once_with(123, 101)
    cog.apply_verified_roles.assert_awaited_once_with(member, "EUN1", leagues)


async def test_rank_refresh_loads_only_configured_guild_records() -> None:
    guild = SimpleNamespace(id=123)
    verifications = SimpleNamespace(
        purge_access_logs=AsyncMock(),
        list_for_guild=AsyncMock(return_value=[]),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_access_log_retention_days=30,
        ),
        get_guild=Mock(return_value=guild),
        verifications=verifications,
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot

    await LegacyVerificationCog.refresh_verified.coro(cog)

    bot.get_guild.assert_called_once_with(123)
    verifications.purge_access_logs.assert_awaited_once_with(123, 30)
    verifications.list_for_guild.assert_awaited_once_with(123)


async def test_remove_own_verification_deletes_link_and_audit_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    class FakeMessageable:
        def __init__(self) -> None:
            self.delete = AsyncMock()
            self.get_partial_message = Mock(return_value=SimpleNamespace(delete=self.delete))

    member = FakeMember(101)
    channel = FakeMessageable()
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(guild_id=123, user=member, guild=guild, response=response)
    link = SimpleNamespace(message_id=456)
    bot = SimpleNamespace(
        settings=SimpleNamespace(zweryfikowani_channel_id=789),
        verifications=SimpleNamespace(delete_by_user=AsyncMock(return_value=link)),
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification_legacy.discord, "Member", FakeMember)
    monkeypatch.setattr(verification_legacy.discord.abc, "Messageable", FakeMessageable)
    monkeypatch.setattr(verification_legacy, "_remove_verified_marker", remove_marker)

    await LegacyVerificationCog.remove_own_verification.callback(cog, interaction)

    bot.verifications.delete_by_user.assert_awaited_once_with(123, 101)
    bot.verification_sessions.cancel_for_user.assert_awaited_once_with(123, 101)
    remove_marker.assert_awaited_once_with(
        bot,
        member,
        reason="Usunięcie weryfikacji przez użytkownika",
    )
    guild.get_channel.assert_called_once_with(789)
    channel.get_partial_message.assert_called_once_with(456)
    channel.delete.assert_awaited_once_with()
    response.send_message.assert_awaited_once_with(
        "Usunięto powiązanie konta Riot. Role regionu, rangi i użytkownika pozostają bez zmian.",
        ephemeral=True,
    )


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
