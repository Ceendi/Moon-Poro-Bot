from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from moon_poro.cogs import message_moderation
from moon_poro.cogs.message_moderation import MessageModerationCog


def make_cog(*, clash: bool = False, boost: bool = False) -> MessageModerationCog:
    settings = SimpleNamespace(
        clash_filter_enabled=clash,
        szukanie_gry_channel_id=10,
        boost_alert_enabled=boost,
        boost_keywords=["boost", "tanio"],
        mod_alert_channel_id=20,
    )
    bot = SimpleNamespace(settings=settings, add_view=Mock())
    return MessageModerationCog(bot)


def make_message(content: str, *, channel_id: int = 10) -> SimpleNamespace:
    author = SimpleNamespace(bot=False, id=7, send=AsyncMock())
    channel = SimpleNamespace(id=channel_id, mention=f"<#{channel_id}>")
    return SimpleNamespace(
        author=author,
        guild=SimpleNamespace(),
        channel=channel,
        content=content,
        delete=AsyncMock(),
        jump_url="https://discord.example/messages/1",
    )


async def test_on_message_ignores_bots() -> None:
    cog = make_cog(clash=True, boost=True)
    cog._handle_clash = AsyncMock()
    cog._send_boost_alert = AsyncMock()
    message = make_message("clash boost")
    message.author.bot = True

    await cog.on_message(message)

    cog._handle_clash.assert_not_awaited()
    cog._send_boost_alert.assert_not_awaited()


async def test_on_message_prioritizes_clash_rule() -> None:
    cog = make_cog(clash=True, boost=True)
    cog._handle_clash = AsyncMock()
    cog._send_boost_alert = AsyncMock()
    message = make_message("Szukamy do CLASH, będzie boost")

    await cog.on_message(message)

    cog._handle_clash.assert_awaited_once_with(message)
    cog._send_boost_alert.assert_not_awaited()


async def test_on_message_sends_boost_alert_for_keyword() -> None:
    cog = make_cog(boost=True)
    cog._send_boost_alert = AsyncMock()
    message = make_message("Wbiję rangę TANIO", channel_id=11)

    await cog.on_message(message)

    cog._send_boost_alert.assert_awaited_once_with(message)


async def test_handle_clash_deletes_message_and_notifies_author() -> None:
    cog = make_cog()
    message = make_message("clash")

    await cog._handle_clash(message)

    message.delete.assert_awaited_once_with()
    message.author.send.assert_awaited_once()


async def test_handle_clash_stops_when_delete_is_forbidden() -> None:
    cog = make_cog()
    message = make_message("clash")
    response = Mock(status=403, reason="Forbidden")
    message.delete.side_effect = discord.Forbidden(response, "cannot delete")

    await cog._handle_clash(message)

    message.author.send.assert_not_awaited()


async def test_handle_clash_suppresses_forbidden_dm() -> None:
    cog = make_cog()
    message = make_message("clash")
    response = Mock(status=403, reason="Forbidden")
    message.author.send.side_effect = discord.Forbidden(response, "cannot dm")

    await cog._handle_clash(message)

    message.delete.assert_awaited_once_with()


async def test_send_boost_alert_contains_audit_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMessageable:
        def __init__(self) -> None:
            self.send = AsyncMock()

    monkeypatch.setattr(message_moderation.discord.abc, "Messageable", FakeMessageable)
    cog = make_cog()
    alert_channel = FakeMessageable()
    message = make_message("boost offer")
    message.guild.get_channel = Mock(return_value=alert_channel)

    await cog._send_boost_alert(message)

    alert_channel.send.assert_awaited_once()
    embed = alert_channel.send.await_args.kwargs["embed"]
    assert embed.title == "⚠️ Możliwa oferta boostingu"
    assert any(field.name == "Link" for field in embed.fields)
    assert alert_channel.send.await_args.kwargs["allowed_mentions"].everyone is False
