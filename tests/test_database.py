from collections.abc import Callable
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro import database
from moon_poro.settings import Settings


def test_database_url_preserves_connection_settings(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        postgres_user="user name",
        postgres_password="p@ss%word",  # pragma: allowlist secret
        postgres_host="db.internal",
        postgres_port=5544,
        postgres_db="moon poro",
    )

    url = database.make_database_url(settings)

    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "user name"
    assert url.password == "p@ss%word"  # pragma: allowlist secret
    assert url.host == "db.internal"
    assert url.port == 5544
    assert url.database == "moon poro"


async def test_database_close_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: Callable[..., Settings],
) -> None:
    engine = SimpleAsyncEngine()
    session_factory = Mock()
    monkeypatch.setattr(database, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(database, "async_sessionmaker", Mock(return_value=session_factory))

    connection = database.Database(settings_factory())
    await connection.close()

    engine.dispose.assert_awaited_once_with()


async def test_upgrade_database_runs_blocking_migration_in_thread(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: Callable[..., Settings],
) -> None:
    to_thread = AsyncMock()
    monkeypatch.setattr(database.asyncio, "to_thread", to_thread)
    settings = settings_factory()

    await database.upgrade_database(settings)

    to_thread.assert_awaited_once_with(database._upgrade_database, settings)


def test_upgrade_database_configures_project_migration(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: Callable[..., Settings],
) -> None:
    config = Mock()
    config.attributes = {}
    config_class = Mock(return_value=config)
    upgrade = Mock()
    monkeypatch.setattr(database, "Config", config_class)
    monkeypatch.setattr(database.command, "upgrade", upgrade)
    settings = settings_factory(postgres_password="percent%password")  # pragma: allowlist secret

    database._upgrade_database(settings)

    config_class.assert_called_once()
    options = {call.args[0]: call.args[1] for call in config.set_main_option.call_args_list}
    assert options["script_location"].endswith("alembic")
    assert "%%25" in options["sqlalchemy.url"]
    assert config.attributes["guild_id"] == settings.guild_id
    upgrade.assert_called_once_with(config, "head")


class SimpleAsyncEngine:
    def __init__(self) -> None:
        self.dispose = AsyncMock()
