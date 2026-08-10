from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro import app


class RiotClientContext:
    def __init__(self) -> None:
        self.client = object()

    async def __aenter__(self) -> object:
        return self.client

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_configure_logging_uses_hardened_service_log_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rotating = Mock()
    rotating_handler = Mock(return_value=rotating)
    monkeypatch.setenv("MOON_PORO_LOG_FILE", "/var/log/moon-poro/discord.log")
    monkeypatch.setattr(app.logging.handlers, "RotatingFileHandler", rotating_handler)
    monkeypatch.setattr(app.logging, "basicConfig", Mock())

    app.configure_logging()

    rotating_handler.assert_called_once_with(
        "/var/log/moon-poro/discord.log",
        encoding="utf-8",
        maxBytes=8 * 1024 * 1024,
        backupCount=2,
    )
    rotating.setFormatter.assert_called_once()


async def test_main_starts_and_closes_all_resources(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory,
) -> None:
    settings = settings_factory()
    database = SimpleNamespace(close=AsyncMock())
    bot = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    riot_context = RiotClientContext()
    bot_class = Mock(return_value=bot)
    monkeypatch.setattr(app, "configure_logging", Mock())
    monkeypatch.setattr(app, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(app, "upgrade_database", AsyncMock())
    monkeypatch.setattr(app, "Database", Mock(return_value=database))
    monkeypatch.setattr(app, "RiotAPIClient", Mock(return_value=riot_context))
    monkeypatch.setattr(app, "MoonPoroBot", bot_class)
    monkeypatch.setattr(app, "create_intents", Mock(return_value="intents"))

    await app.main()

    app.upgrade_database.assert_awaited_once_with(settings)
    assert bot_class.call_args.kwargs["riot_client"] is riot_context.client
    bot.start.assert_awaited_once_with(settings.discord_token.get_secret_value())
    bot.close.assert_awaited_once_with()
    database.close.assert_awaited_once_with()


async def test_main_closes_resources_when_bot_fails(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory,
) -> None:
    settings = settings_factory()
    database = SimpleNamespace(close=AsyncMock())
    bot = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("failed")), close=AsyncMock())
    monkeypatch.setattr(app, "configure_logging", Mock())
    monkeypatch.setattr(app, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(app, "upgrade_database", AsyncMock())
    monkeypatch.setattr(app, "Database", Mock(return_value=database))
    monkeypatch.setattr(app, "RiotAPIClient", Mock(return_value=RiotClientContext()))
    monkeypatch.setattr(app, "MoonPoroBot", Mock(return_value=bot))
    monkeypatch.setattr(app, "create_intents", Mock(return_value="intents"))

    with pytest.raises(RuntimeError, match="failed"):
        await app.main()

    bot.close.assert_awaited_once_with()
    database.close.assert_awaited_once_with()
