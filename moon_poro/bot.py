from __future__ import annotations

import logging

import discord
from discord.ext import commands
from pulsefire.clients import RiotAPIClient

from moon_poro.database import Database
from moon_poro.repositories import (
    GuildFeatureRepository,
    ModerationStatsRepository,
    VerificationRepository,
    WarningRepository,
)
from moon_poro.responses import install_error_handler
from moon_poro.riot import RiotAPIMonitor, RiotAuthBreaker
from moon_poro.settings import Settings
from moon_poro.verification_sessions import VerificationSessionRepository

logger = logging.getLogger("moon_poro.bot")


class MoonPoroBot(commands.Bot):
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        riot_client: RiotAPIClient,
        riot_monitor: RiotAPIMonitor,
        riot_auth_breaker: RiotAuthBreaker | None = None,
        intents: discord.Intents,
    ) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.database = database
        self.riot_client = riot_client
        self.riot_monitor = riot_monitor
        self.riot_auth_breaker = riot_auth_breaker or RiotAuthBreaker(
            probe_interval_seconds=settings.rank_refresh_auth_probe_interval_seconds
        )
        self.verifications = VerificationRepository(database.session_factory)
        self.verification_sessions = VerificationSessionRepository(database.session_factory)
        self.warnings = WarningRepository(database.session_factory)
        self.moderation_stats = ModerationStatsRepository(database.session_factory)
        self.guild_features = GuildFeatureRepository(database.session_factory)

    async def setup_hook(self) -> None:
        extensions = ["core_events"]
        if self.settings.roles_enabled:
            extensions.append("roles")
        if self.settings.verification_enabled:
            extensions.append(
                "verification_legacy"
                if self.settings.verification_mode == "legacy_icon"
                else "verification"
            )
        if self.settings.warnings_enabled:
            extensions.append("warnings")
        if self.settings.mod_stats_enabled:
            extensions.append("mod_stats")
        if self.settings.requires_message_content:
            extensions.append("message_moderation")

        for extension in extensions:
            await self.load_extension(f"moon_poro.cogs.{extension}")

        install_error_handler(self)
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Loaded features: %s", ", ".join(extensions))

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")


def create_intents(settings: Settings) -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.moderation = True
    if settings.requires_message_content:
        intents.guild_messages = True
        intents.messages = True
        intents.message_content = True
    return intents
