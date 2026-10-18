from moon_poro.bot import create_intents


def test_create_intents_keeps_privileged_message_content_disabled(settings_factory) -> None:
    intents = create_intents(settings_factory())

    assert intents.guilds
    assert intents.members
    assert intents.moderation
    assert not intents.message_content


def test_create_intents_enables_messages_for_moderation_features(settings_factory) -> None:
    settings = settings_factory(
        boost_alert_enabled=True,
        mod_alert_channel_id=4,
    )

    intents = create_intents(settings)

    assert intents.guild_messages
    assert intents.messages
    assert intents.message_content


async def test_rank_emojis_load_from_the_application_and_failures_are_harmless() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord

    from moon_poro.bot import MoonPoroBot

    class FakeEmoji:
        def __init__(self, name: str) -> None:
            self.name = name

        def __str__(self) -> str:
            return f"<:{self.name}:1>"

    bot = SimpleNamespace(
        rank_emojis={},
        fetch_application_emojis=AsyncMock(return_value=[FakeEmoji("rank_gold")]),
    )
    await MoonPoroBot._load_rank_emojis(bot)  # type: ignore[arg-type]
    assert bot.rank_emojis == {"GOLD": "<:rank_gold:1>"}

    failing = SimpleNamespace(
        rank_emojis={},
        fetch_application_emojis=AsyncMock(
            side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="x"), "down")
        ),
    )
    await MoonPoroBot._load_rank_emojis(failing)  # type: ignore[arg-type]
    assert failing.rank_emojis == {}
