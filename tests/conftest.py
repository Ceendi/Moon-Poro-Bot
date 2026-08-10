from __future__ import annotations

from collections.abc import Callable

import pytest

from moon_poro.settings import Settings


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "discord_token": "discord-test-token",
        "riot_api_token": "riot-test-token",
        "postgres_user": "bot",
        "postgres_password": "database-test-password",  # pragma: allowlist secret
        "postgres_host": "127.0.0.1",
        "postgres_db": "bot",
        "guild_id": 123,
        "warn_channel_id": 1,
        "zweryfikowani_channel_id": 2,
        "komendy_botowe_channel_id": 3,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return build_settings
