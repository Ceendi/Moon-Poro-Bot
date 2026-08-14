# The temporary profile-icon flow intentionally parallels the RSO cog so it can
# be removed cleanly after the RSO rollout without coupling both implementations.
# pylint: disable=duplicate-code
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy.exc import IntegrityError

from moon_poro.bot import MoonPoroBot
from moon_poro.cogs.verification import (
    VerificationCog,
    _apply_verified_roles,
    _get_leagues,
)
from moon_poro.permissions import administrator_only
from moon_poro.riot import (
    API_SERVERS,
    SERVER_TRANSLATION,
    RiotAPIUnavailable,
    get_rank_from_leagues,
    profile_icon_url,
    riot_api_call,
)
from moon_poro.roles import find_role, member_has_role, member_roles_named

logger = logging.getLogger("moon_poro.verification_legacy")

type RiotPayload = dict[str, Any]

RIOT_GAME_NAME_MIN_LENGTH = 3
RIOT_GAME_NAME_MAX_LENGTH = 16
RIOT_TAG_LINE_MIN_LENGTH = 3
RIOT_TAG_LINE_MAX_LENGTH = 5


def _normalize_riot_id_parts(game_name: str, tag_line: str) -> tuple[str, str]:
    normalized_name = game_name.strip()
    normalized_tag = tag_line.strip().removeprefix("#").strip()
    return normalized_name, normalized_tag


def _riot_id_validation_error(game_name: str, tag_line: str) -> str | None:
    if not RIOT_GAME_NAME_MIN_LENGTH <= len(game_name) <= RIOT_GAME_NAME_MAX_LENGTH:
        return "Nazwa w Riot ID musi mieć od 3 do 16 znaków."
    if "#" in game_name:
        return "W polu nazwy wpisz tylko część Riot ID przed znakiem `#`."
    if not RIOT_TAG_LINE_MIN_LENGTH <= len(tag_line) <= RIOT_TAG_LINE_MAX_LENGTH:
        return "Tag w Riot ID musi mieć od 3 do 5 znaków."
    if not tag_line.isalnum():
        return "Tag w Riot ID może zawierać wyłącznie litery i cyfry."
    return None


async def _get_account(
    bot: MoonPoroBot, game_name: str, tag_line: str, platform: str
) -> RiotPayload | None:
    return await riot_api_call(
        lambda: bot.riot_client.get_account_v1_by_riot_id(
            game_name=game_name.strip(),
            tag_line=tag_line.replace("#", "").strip(),
            region=API_SERVERS[platform],
        ),
        not_found=None,
    )


async def _get_summoner(bot: MoonPoroBot, platform: str, puuid: str) -> RiotPayload | None:
    return await riot_api_call(
        lambda: bot.riot_client.get_lol_summoner_v4_by_puuid(region=platform, puuid=puuid),
        not_found=None,
    )


async def _remove_verified_marker(bot: MoonPoroBot, member: discord.Member, *, reason: str) -> None:
    verified_role = find_role(member.guild, bot.settings.verified_role_name, bot.settings)
    if verified_role is not None and verified_role in member.roles:
        await member.remove_roles(verified_role, reason=reason)


class LegacyVerificationStartView(discord.ui.View):
    def __init__(self, bot: MoonPoroBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.cooldowns: commands.CooldownMapping[discord.Interaction] = (
            commands.CooldownMapping.from_cooldown(
                1.0,
                bot.settings.verification_cooldown,
                lambda interaction: interaction.user.id,
            )
        )

    @discord.ui.button(
        label="Zweryfikuj konto Riot",
        emoji="✅",
        style=discord.ButtonStyle.green,
        custom_id="verification:start:profile-icon:v1",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[LegacyVerificationStartView],
    ) -> None:
        if interaction.guild_id != self.bot.settings.guild_id:
            await interaction.response.send_message(
                "Ten panel nie należy do skonfigurowanego serwera.", ephemeral=True
            )
            return
        retry_after = self.cooldowns.update_rate_limit(interaction)
        if retry_after:
            await interaction.response.send_message(
                f"Spróbuj ponownie za {int(retry_after)} s.", ephemeral=True
            )
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Weryfikację rozpocznij na serwerze Discord.", ephemeral=True
            )
            return
        if member_has_role(
            interaction.user,
            self.bot.settings.verified_role_name,
            self.bot.settings,
        ) or await self.bot.verifications.get_by_user(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message(
                "To konto Discord jest już zweryfikowane. Użyj `/usun_weryfikacje`, "
                "aby usunąć powiązanie.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(LegacyVerificationModal(self.bot))


class LegacyVerificationModal(discord.ui.Modal, title="Weryfikacja konta Riot"):
    def __init__(
        self,
        bot: MoonPoroBot,
        *,
        game_name: str = "",
        tag_line: str = "",
        platform: str | None = None,
    ) -> None:
        super().__init__(
            timeout=bot.settings.verification_timeout,
            custom_id="verification:legacy-account:v2",
        )
        self.bot = bot
        self.game_name: discord.ui.TextInput[LegacyVerificationModal] = discord.ui.TextInput(
            custom_id="verification:riot-game-name:v2",
            placeholder="np. Moon Poro",
            default=game_name or None,
            min_length=RIOT_GAME_NAME_MIN_LENGTH,
            max_length=RIOT_GAME_NAME_MAX_LENGTH,
        )
        self.tag_line: discord.ui.TextInput[LegacyVerificationModal] = discord.ui.TextInput(
            custom_id="verification:riot-tag-line:v2",
            placeholder="Część po #",
            default=tag_line or None,
            min_length=RIOT_TAG_LINE_MIN_LENGTH,
            # Six characters let us tolerate an accidentally pasted leading '#'.
            max_length=RIOT_TAG_LINE_MAX_LENGTH + 1,
        )
        self.platform: discord.ui.Select[LegacyVerificationModal] = discord.ui.Select(
            custom_id="verification:platform:v2",
            placeholder="Wybierz serwer",
            min_values=1,
            max_values=1,
            required=True,
            options=[
                discord.SelectOption(
                    label="EUNE",
                    value="EUN1",
                    description="Europa Północna i Wschodnia",
                    default=platform == "EUN1",
                ),
                discord.SelectOption(
                    label="EUW",
                    value="EUW1",
                    description="Europa Zachodnia",
                    default=platform == "EUW1",
                ),
                discord.SelectOption(
                    label="NA",
                    value="NA1",
                    description="Ameryka Północna",
                    default=platform == "NA1",
                ),
            ],
        )
        self.add_item(
            discord.ui.Label(
                text="Nazwa Riot ID",
                description="Część przed #",
                component=self.game_name,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Tag Riot ID",
                description="Część po #, od 3 do 5 liter lub cyfr",
                component=self.tag_line,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Serwer LoL",
                component=self.platform,
            )
        )

    def _retry_view(
        self, interaction: discord.Interaction, game_name: str, tag_line: str, platform: str
    ) -> LegacyVerificationRetryView:
        return LegacyVerificationRetryView(
            self.bot,
            owner_id=interaction.user.id,
            game_name=game_name,
            tag_line=tag_line,
            platform=platform,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.bot.settings.guild_id:
            await interaction.response.send_message(
                "Weryfikacja jest dostępna wyłącznie na skonfigurowanym serwerze.",
                ephemeral=True,
            )
            return
        if len(self.platform.values) != 1 or self.platform.values[0] not in SERVER_TRANSLATION:
            await interaction.response.send_message(
                "Wybierz jeden z dostępnych serwerów League of Legends.", ephemeral=True
            )
            return

        platform = self.platform.values[0]
        game_name, tag_line = _normalize_riot_id_parts(
            self.game_name.value,
            self.tag_line.value,
        )
        validation_error = _riot_id_validation_error(game_name, tag_line)
        if validation_error is not None:
            await interaction.response.send_message(
                f"❌ {validation_error}",
                view=self._retry_view(interaction, game_name, tag_line, platform),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            account = await _get_account(
                self.bot,
                game_name,
                tag_line,
                platform,
            )
            if account is None:
                await interaction.followup.send(
                    f"Nie znaleziono Riot ID **{discord.utils.escape_markdown(game_name)}#"
                    f"{discord.utils.escape_markdown(tag_line)}** w regionie "
                    f"**{SERVER_TRANSLATION[platform]}**. Sprawdź dane z profilu Riot "
                    "(nie nazwę logowania) oraz wybrany serwer LoL.",
                    view=self._retry_view(interaction, game_name, tag_line, platform),
                    ephemeral=True,
                )
                return
            puuid = str(account["puuid"])
            if await self.bot.verifications.get_by_puuid(interaction.guild_id, puuid):
                await interaction.followup.send(
                    "To konto Riot jest już połączone z innym kontem Discord.", ephemeral=True
                )
                return
            summoner = await _get_summoner(self.bot, platform, puuid)
        except RiotAPIUnavailable:
            await interaction.followup.send(
                "Riot API jest chwilowo niedostępne. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return
        if summoner is None:
            await interaction.followup.send(
                "Riot ID istnieje, ale nie ma profilu League of Legends w wybranym regionie. "
                "Sprawdź wybrany serwer.",
                view=self._retry_view(interaction, game_name, tag_line, platform),
                ephemeral=True,
            )
            return

        icon_id = secrets.choice(range(29))
        embed = discord.Embed(
            title="Potwierdź konto przez ikonę profilu",
            description=(
                "Ustaw w kliencie League of Legends ikonę pokazaną poniżej, "
                "a następnie kliknij **Sprawdź ikonę**."
            ),
            colour=discord.Colour.from_rgb(116, 211, 224),
        )
        embed.add_field(
            name="Riot ID",
            value=f"{account.get('gameName', game_name)}#{account.get('tagLine', tag_line)}",
            inline=False,
        )
        embed.add_field(name="Region", value=SERVER_TRANSLATION[platform])
        embed.set_thumbnail(url=profile_icon_url(icon_id))
        view = LegacyIconConfirmationView(
            self.bot,
            owner_id=interaction.user.id,
            platform=platform,
            puuid=puuid,
            game_name=str(account.get("gameName", game_name)),
            tag_line=str(account.get("tagLine", tag_line)),
            expected_icon_id=icon_id,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class LegacyVerificationRetryView(discord.ui.View):
    def __init__(
        self,
        bot: MoonPoroBot,
        *,
        owner_id: int,
        game_name: str,
        tag_line: str,
        platform: str,
    ) -> None:
        super().__init__(timeout=bot.settings.view_timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.game_name = game_name
        self.tag_line = tag_line
        self.platform = platform

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Ten przycisk należy do innego użytkownika.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Popraw dane", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def retry(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[LegacyVerificationRetryView],
    ) -> None:
        await interaction.response.send_modal(
            LegacyVerificationModal(
                self.bot,
                game_name=self.game_name,
                tag_line=self.tag_line,
                platform=self.platform,
            )
        )


class LegacyIconConfirmationView(discord.ui.View):
    def __init__(
        self,
        bot: MoonPoroBot,
        *,
        owner_id: int,
        platform: str,
        puuid: str,
        game_name: str,
        tag_line: str,
        expected_icon_id: int,
    ) -> None:
        super().__init__(timeout=bot.settings.verification_timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.platform = platform
        self.puuid = puuid
        self.game_name = game_name
        self.tag_line = tag_line
        self.expected_icon_id = expected_icon_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Ten przycisk należy do innego użytkownika.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Sprawdź ikonę", emoji="🔎", style=discord.ButtonStyle.green)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[LegacyIconConfirmationView],
    ) -> None:
        if (
            interaction.guild is None
            or interaction.guild_id != self.bot.settings.guild_id
            or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message(
                "Weryfikację dokończ na skonfigurowanym serwerze.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if await self.bot.verifications.get_by_user(interaction.guild_id, self.owner_id):
                await interaction.followup.send(
                    "To konto Discord zostało już zweryfikowane.", ephemeral=True
                )
                return
            if await self.bot.verifications.get_by_puuid(interaction.guild_id, self.puuid):
                await interaction.followup.send(
                    "To konto Riot zostało już przypisane do innego użytkownika.",
                    ephemeral=True,
                )
                return
            summoner = await _get_summoner(self.bot, self.platform, self.puuid)
            if summoner is None or int(summoner.get("profileIconId", -1)) != self.expected_icon_id:
                await interaction.followup.send(
                    "Ikona profilu nie została jeszcze zmieniona. Ustaw wskazaną ikonę "
                    "i spróbuj ponownie.",
                    ephemeral=True,
                )
                return
            leagues = await _get_leagues(self.bot, self.platform, self.puuid)
        except RiotAPIUnavailable:
            await interaction.followup.send(
                "Riot API jest chwilowo niedostępne. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return

        rank = get_rank_from_leagues(leagues).title()
        channel_id = self.bot.settings.zweryfikowani_channel_id
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send(
                "Kanał audytowy weryfikacji jest niedostępny. Zgłoś to administratorowi.",
                ephemeral=True,
            )
            return

        audit_embed = discord.Embed(
            title="Weryfikacja konta: ikona profilu",
            colour=discord.Colour.green(),
        )
        audit_embed.add_field(
            name="Użytkownik", value=f"<@{interaction.user.id}> (`{interaction.user.id}`)"
        )
        audit_embed.add_field(name="Region", value=SERVER_TRANSLATION[self.platform])
        audit_embed.add_field(name="Ranga Solo/Duo", value=rank)
        audit_embed.add_field(
            name="Riot ID", value=f"{self.game_name}#{self.tag_line}", inline=False
        )
        try:
            audit_message = await channel.send(
                embed=audit_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Nie udało się zapisać audytu weryfikacji. Spróbuj ponownie.", ephemeral=True
            )
            return

        try:
            await self.bot.verifications.create(
                guild_id=interaction.guild_id,
                user_id=self.owner_id,
                message_id=audit_message.id,
                platform=self.platform,
                puuid=self.puuid,
                method="PROFILE_ICON",
            )
            cog = self.bot.get_cog("LegacyVerificationCog")
            if isinstance(cog, LegacyVerificationCog):
                await cog.apply_verified_roles(interaction.user, self.platform, leagues)
            else:
                await _apply_verified_roles(self.bot, interaction.user, self.platform, leagues)
        except (IntegrityError, discord.HTTPException):
            logger.exception("Could not finish profile-icon verification for %s", self.owner_id)
            await self.bot.verifications.delete_by_user(interaction.guild_id, self.owner_id)
            with suppress(discord.NotFound, discord.HTTPException):
                await audit_message.delete()
            await interaction.followup.send(
                "Nie udało się bezpiecznie zakończyć weryfikacji. Spróbuj ponownie.",
                ephemeral=True,
            )
            return

        button.disabled = True
        with suppress(discord.HTTPException):
            await interaction.edit_original_response(view=self)
        await interaction.followup.send("✅ Weryfikacja zakończona.", ephemeral=True)


class LegacyVerificationCog(VerificationCog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self._managed_role_updates: set[int] = set()
        bot.add_view(LegacyVerificationStartView(bot))
        self.refresh_verified.change_interval(hours=bot.settings.rank_refresh_interval_hours)
        self.refresh_verified.start()

    async def cog_unload(self) -> None:
        self.refresh_verified.cancel()

    async def apply_verified_roles(
        self,
        member: discord.Member,
        platform: str,
        leagues: list[RiotPayload],
    ) -> None:
        self._managed_role_updates.add(member.id)
        try:
            await _apply_verified_roles(self.bot, member, platform, leagues)
        finally:
            self._managed_role_updates.discard(member.id)

    @tasks.loop(hours=24, reconnect=True)
    async def refresh_verified(self) -> None:  # type: ignore[override]
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            return
        try:
            await self.bot.verifications.purge_access_logs(
                guild.id, self.bot.settings.verification_access_log_retention_days
            )
            links = await self.bot.verifications.list_for_guild(guild.id)
        except Exception:
            logger.exception("Could not load profile verification refresh data")
            return
        for link in links:
            member = guild.get_member(link.discord_user_id)
            if member is None or not link.puuid:
                continue
            try:
                leagues = await _get_leagues(self.bot, link.platform, link.puuid)
                await self.apply_verified_roles(member, link.platform, leagues)
            except (RiotAPIUnavailable, discord.HTTPException):
                logger.exception("Could not refresh verification roles for %s", member.id)
            await asyncio.sleep(0.6)

    @refresh_verified.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    @refresh_verified.error
    async def refresh_error(self, error: BaseException) -> None:
        logger.exception("Profile verification refresh loop failed", exc_info=error)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != self.bot.settings.guild_id:
            return
        link = await self.bot.verifications.get_by_user(member.guild.id, member.id)
        if link is None or not link.puuid:
            return
        try:
            leagues = await _get_leagues(self.bot, link.platform, link.puuid)
            await self.apply_verified_roles(member, link.platform, leagues)
        except (RiotAPIUnavailable, discord.HTTPException):
            logger.exception("Could not restore verification roles for %s", member.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.guild.id != self.bot.settings.guild_id or after.id in self._managed_role_updates:
            return
        protected_names = set(
            self.bot.settings.lol_ranks
            + self.bot.settings.lol_servers
            + [
                self.bot.settings.verified_role_name,
                self.bot.settings.member_role_name,
            ]
        )
        before_ids = {
            role.id for role in member_roles_named(before, protected_names, self.bot.settings)
        }
        after_ids = {
            role.id for role in member_roles_named(after, protected_names, self.bot.settings)
        }
        if before_ids == after_ids:
            return
        link = await self.bot.verifications.get_by_user(after.guild.id, after.id)
        if link is None or not link.puuid:
            return
        try:
            leagues = await _get_leagues(self.bot, link.platform, link.puuid)
            await self.apply_verified_roles(after, link.platform, leagues)
        except (RiotAPIUnavailable, discord.HTTPException):
            logger.exception("Could not restore protected verification roles for %s", after.id)

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="weryfikacja", description="Publikuje panel weryfikacji ikoną")
    async def publish_verification(  # type: ignore[override]
        self, interaction: discord.Interaction
    ) -> None:
        content = (
            "**Weryfikacja konta League of Legends**\n"
            "Kliknij przycisk, podaj Riot ID i region, a następnie ustaw wskazaną ikonę "
            "profilu w kliencie League of Legends. Bot zapisze powiązanie z Discordem "
            "i będzie synchronizował region oraz rangę Solo/Duo."
        )
        await interaction.response.send_message(content, view=LegacyVerificationStartView(self.bot))

    @app_commands.command(
        name="usun_weryfikacje", description="Usuwa Twoje powiązanie z kontem Riot"
    )
    async def remove_own_verification(  # type: ignore[override]
        self, interaction: discord.Interaction
    ) -> None:
        guild_id = interaction.guild_id or 0
        link = await self.bot.verifications.delete_by_user(guild_id, interaction.user.id)
        await self.bot.verification_sessions.cancel_for_user(guild_id, interaction.user.id)
        if interaction.guild is not None and isinstance(interaction.user, discord.Member):
            await _remove_verified_marker(
                self.bot,
                interaction.user,
                reason="Usunięcie weryfikacji przez użytkownika",
            )
            channel_id = self.bot.settings.zweryfikowani_channel_id
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
            if link and link.message_id and isinstance(channel, discord.abc.Messageable):
                with suppress(discord.NotFound, discord.HTTPException):
                    await channel.get_partial_message(link.message_id).delete()
        await interaction.response.send_message(
            "Usunięto powiązanie konta Riot. Role regionu, rangi i użytkownika pozostają bez zmian.",
            ephemeral=True,
        )


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(LegacyVerificationCog(bot))
