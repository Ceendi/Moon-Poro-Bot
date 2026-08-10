from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os

from pulsefire.clients import RiotAPIClient

from moon_poro.bot import MoonPoroBot, create_intents
from moon_poro.database import Database, upgrade_database
from moon_poro.settings import Settings


def configure_logging() -> None:
    formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating_file = logging.handlers.RotatingFileHandler(
        os.getenv("MOON_PORO_LOG_FILE", "discord.log"),
        encoding="utf-8",
        maxBytes=8 * 1024 * 1024,
        backupCount=2,
    )
    rotating_file.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, rotating_file])
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


async def main() -> None:
    configure_logging()
    settings = Settings()
    await upgrade_database(settings)
    database = Database(settings)

    try:
        async with RiotAPIClient(
            default_headers={"X-Riot-Token": settings.riot_api_token.get_secret_value()}
        ) as riot_client:
            bot = MoonPoroBot(
                settings=settings,
                database=database,
                riot_client=riot_client,
                intents=create_intents(settings),
            )
            try:
                await bot.start(settings.discord_token.get_secret_value())
            finally:
                await bot.close()
    finally:
        await database.close()


def run() -> None:
    asyncio.run(main())
