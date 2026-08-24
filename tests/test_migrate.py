from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro import migrate
from moon_poro.settings import Settings


@pytest.mark.parametrize("env_file", [None, Path("/etc/moon-poro/bot.env")])
async def test_run_migrations_loads_settings_and_upgrades_database(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: Callable[..., Settings],
    env_file: Path | None,
) -> None:
    settings = settings_factory()
    settings_class = Mock(return_value=settings)
    upgrade_database = AsyncMock()
    monkeypatch.setattr(migrate, "Settings", settings_class)
    monkeypatch.setattr(migrate, "upgrade_database", upgrade_database)

    await migrate.run_migrations(env_file, legacy_audit_channel_id=987)

    if env_file is None:
        settings_class.assert_called_once_with()
    else:
        settings_class.assert_called_once_with(_env_file=env_file)
    upgrade_database.assert_awaited_once_with(settings, legacy_audit_channel_id=987)
