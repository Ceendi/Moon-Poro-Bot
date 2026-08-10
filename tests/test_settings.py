import pytest
from pydantic import ValidationError

from moon_poro.settings import Settings


def make_settings(**overrides):
    values = {
        "discord_token": "discord-secret",
        "riot_api_token": "riot-secret",
        "postgres_user": "bot",
        "postgres_password": "db-secret",
        "postgres_host": "127.0.0.1",
        "postgres_db": "bot",
        "guild_id": 123,
        "warn_channel_id": 1,
        "zweryfikowani_channel_id": 2,
        "komendy_botowe_channel_id": 3,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_message_content_is_disabled_for_core_features() -> None:
    settings = make_settings()
    assert settings.requires_message_content is False


def test_message_content_is_enabled_for_clash_filter() -> None:
    settings = make_settings(clash_filter_enabled=True, szukanie_gry_channel_id=4)
    assert settings.requires_message_content is True


def test_role_allowlist_contains_all_configured_groups() -> None:
    settings = make_settings()
    assert "Unranked" in settings.allowed_role_names
    assert "EUNE" in settings.allowed_role_names
    assert "Support" in settings.allowed_role_names
    assert "Dyskusje" in settings.allowed_role_names


def test_role_ids_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="Role IDs must be positive"):
        make_settings(role_ids={"Zweryfikowany": 0})


def test_warning_configuration_requires_all_levels() -> None:
    with pytest.raises(ValidationError, match="exactly levels 1, 2 and 3"):
        make_settings(warn_roles={1: "Warn"})
