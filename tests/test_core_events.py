from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.cogs import core_events
from moon_poro.cogs.core_events import ACCOUNT_AGE_FEATURE, CoreEventsCog


def make_cog(*, enabled: bool = True) -> CoreEventsCog:
    settings = SimpleNamespace(
        account_age_gate_enabled=enabled,
        minimum_account_age_days=90,
        member_logs_enabled=True,
        komendy_botowe_channel_id=30,
    )
    bot = SimpleNamespace(
        settings=settings,
        guild_features=SimpleNamespace(get=AsyncMock(return_value=enabled), set=AsyncMock()),
    )
    return CoreEventsCog(bot)


async def test_account_age_feature_uses_guild_override() -> None:
    cog = make_cog(enabled=False)

    assert not await cog._account_age_gate_enabled(123)
    cog.bot.guild_features.get.assert_awaited_once_with(123, ACCOUNT_AGE_FEATURE, False)


async def test_member_join_bans_account_below_minimum_age() -> None:
    cog = make_cog()
    cog._log = AsyncMock()
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(
        bot=False,
        id=7,
        guild=guild,
        created_at=datetime.now(UTC) - timedelta(days=2),
        ban=AsyncMock(),
    )

    await cog.on_member_join(member)

    member.ban.assert_awaited_once()
    assert "młodsze" in member.ban.await_args.kwargs["reason"]
    cog._log.assert_awaited_once()


async def test_member_join_accepts_old_account() -> None:
    cog = make_cog()
    cog._log = AsyncMock()
    member = SimpleNamespace(
        bot=False,
        guild=SimpleNamespace(id=123),
        created_at=datetime.now(UTC) - timedelta(days=365),
        ban=AsyncMock(),
    )

    await cog.on_member_join(member)

    member.ban.assert_not_awaited()
    cog._log.assert_not_awaited()


async def test_log_sends_without_mentions(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMessageable:
        def __init__(self) -> None:
            self.send = AsyncMock()

    monkeypatch.setattr(core_events.discord.abc, "Messageable", FakeMessageable)
    cog = make_cog()
    channel = FakeMessageable()
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))

    await cog._log(guild, "audit event")

    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["allowed_mentions"].everyone is False


async def test_toggle_account_age_gate_persists_inverse() -> None:
    cog = make_cog(enabled=True)
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await CoreEventsCog.toggle_account_age_gate.callback(cog, interaction)

    cog.bot.guild_features.set.assert_awaited_once_with(123, ACCOUNT_AGE_FEATURE, False, 7)
    assert "wyłączona" in interaction.response.send_message.await_args.args[0]
