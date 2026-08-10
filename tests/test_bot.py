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
