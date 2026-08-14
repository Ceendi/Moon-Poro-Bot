from pulsefire.clients import RiotAPIClient

from moon_poro.bot import MoonPoroBot, create_intents
from moon_poro.database import Database
from moon_poro.riot import RiotAPIMonitor
from moon_poro.settings import Settings


async def test_all_enabled_extensions_register_without_conflicts() -> None:
    settings = Settings(
        _env_file=None,
        discord_token="discord-secret",
        riot_api_token="riot-secret",
        postgres_user="bot",
        postgres_password="db-secret",  # pragma: allowlist secret
        postgres_host="127.0.0.1",
        postgres_db="bot",
        guild_id=123,
        warn_channel_id=1,
        zweryfikowani_channel_id=2,
        komendy_botowe_channel_id=3,
        rso_public_base_url="https://bot.example.com",
    )
    database = Database(settings)
    riot_client = RiotAPIClient(default_headers={"X-Riot-Token": "validation-only"})
    bot = MoonPoroBot(
        settings=settings,
        database=database,
        riot_client=riot_client,
        riot_monitor=RiotAPIMonitor(),
        intents=create_intents(settings),
    )
    extensions = ["core_events", "roles", "verification", "warnings", "mod_stats"]

    async with bot:
        for extension in extensions:
            await bot.load_extension(f"moon_poro.cogs.{extension}")

        commands = {command.name: command for command in bot.tree.get_commands()}
        assert set(commands) == {
            "cw",
            "dr",
            "mod_stats",
            "napraw_weryfikacje",
            "nr",
            "pbr",
            "pr",
            "przyznawanie_roli",
            "show_wer_discord",
            "show_wer_user",
            "start",
            "ur",
            "usun_wer_nick",
            "usun_weryfikacje",
            "w",
            "weryfikacja",
            "wylacz_multikonta",
        }
        expected_reasons = {
            "Moderacja",
            "Pomoc użytkownikowi",
            "Podejrzenie multikonta",
            "Korekta danych",
        }
        for command_name in ("show_wer_user", "show_wer_discord"):
            reason = next(
                parameter
                for parameter in commands[command_name].parameters
                if parameter.name == "powod"
            )
            assert {choice.name for choice in reason.choices} == expected_reasons

        for extension in reversed(extensions):
            await bot.unload_extension(f"moon_poro.cogs.{extension}")

    await database.close()


async def test_legacy_verification_reuses_current_management_commands() -> None:
    settings = Settings(
        _env_file=None,
        discord_token="discord-secret",
        riot_api_token="riot-secret",
        postgres_user="bot",
        postgres_password="db-secret",  # pragma: allowlist secret
        postgres_host="127.0.0.1",
        postgres_db="bot",
        guild_id=123,
        warn_channel_id=1,
        zweryfikowani_channel_id=2,
        komendy_botowe_channel_id=3,
        verification_mode="legacy_icon",
        rso_public_base_url=None,
    )
    database = Database(settings)
    riot_client = RiotAPIClient(default_headers={"X-Riot-Token": "validation-only"})
    bot = MoonPoroBot(
        settings=settings,
        database=database,
        riot_client=riot_client,
        riot_monitor=RiotAPIMonitor(),
        intents=create_intents(settings),
    )

    async with bot:
        await bot.load_extension("moon_poro.cogs.verification_legacy")
        commands = {command.name for command in bot.tree.get_commands()}
        assert commands == {
            "show_wer_discord",
            "show_wer_user",
            "usun_wer_nick",
            "usun_weryfikacje",
            "weryfikacja",
        }
        cog = bot.get_cog("LegacyVerificationCog")
        assert cog is not None
        assert not cog.complete_rso_verifications.is_running()
        await bot.unload_extension("moon_poro.cogs.verification_legacy")

    await database.close()
