from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands

from moon_poro.bot import MoonPoroBot
from moon_poro.member_role_lock import member_role_lock
from moon_poro.permissions import administrator_only, moderator_only
from moon_poro.responses import safe_send
from moon_poro.roles import (
    find_role,
    member_has_role,
    member_roles_named,
    role_is_configured,
)
from moon_poro.settings import Settings

logger = logging.getLogger("moon_poro.roles")

ROLE_PANEL_FORBIDDEN_MESSAGE = (
    "Bot nie ma uprawnień, żeby zmienić tę rolę. Daj znać administracji serwera."
)
ROLE_PANEL_ERROR_MESSAGE = (
    "Nie udało się zmienić ról. Spróbuj ponownie za chwilę, "
    "a jeśli problem się powtórzy, daj znać administracji serwera."
)


class RoleCategory(StrEnum):
    SERVER = "server"
    POSITION = "position"
    OPTIONAL = "optional"


def _custom_id(prefix: str, value: str) -> str:
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=8).hexdigest()
    return f"roles:{prefix}:{digest}"


class _RoleState:
    """Track successful HTTP writes without relying on Discord's gateway cache."""

    def __init__(self, member: discord.Member) -> None:
        self.member = member
        self.id = member.id
        self.guild = member.guild
        self.roles = list(member.roles)

    async def add_roles(self, *roles: discord.Role, reason: str) -> None:
        await self.member.add_roles(*roles, reason=reason)
        self.roles.extend(role for role in roles if role not in self.roles)

    async def remove_roles(self, *roles: discord.Role, reason: str) -> None:
        await self.member.remove_roles(*roles, reason=reason)
        self.roles = [role for role in self.roles if role not in roles]


@asynccontextmanager
async def _current_roles(member: discord.Member) -> AsyncIterator[discord.Member]:
    async with member_role_lock(member.guild.id, member.id):
        # Interaction members and the gateway cache can both predate a previous write.
        current = await member.guild.fetch_member(member.id)
        yield cast(discord.Member, _RoleState(current))


def _qualifies_for_member_role(member: discord.Member, settings: Settings) -> bool:
    has_required_roles = bool(member_roles_named(member, settings.lol_servers, settings)) and bool(
        member_roles_named(member, settings.lol_ranks, settings)
    )
    return has_required_roles or member_has_role(member, settings.no_lol_role_name, settings)


async def _update_member_role(bot: MoonPoroBot, member: discord.Member) -> bool:
    """Grant the member role once its requirements are met.

    The member role is never taken away here. Removing a Riot verification keeps
    it on purpose, and a verified account can briefly have no rank role.
    """
    settings = bot.settings
    member_role = find_role(member.guild, settings.member_role_name, settings)
    if member_role is None or member_role in member.roles:
        return False
    if not _qualifies_for_member_role(member, settings):
        return False
    await member.add_roles(member_role, reason="Synchronizacja ról Moon Poro")
    return True


class _RolePanelView(discord.ui.View):
    """Answer the deferred interaction when a role panel callback fails."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
        /,
    ) -> None:
        logger.error(
            "Role panel item %s failed for Discord user %s",
            getattr(item, "custom_id", None),
            interaction.user.id,
            exc_info=error,
        )
        message = (
            ROLE_PANEL_FORBIDDEN_MESSAGE
            if isinstance(error, discord.Forbidden)
            else ROLE_PANEL_ERROR_MESSAGE
        )
        await safe_send(interaction, message)


async def _require_guild(interaction: discord.Interaction) -> discord.Guild | None:
    if interaction.guild is not None:
        return interaction.guild
    await safe_send(interaction, "Ta komenda działa tylko na serwerze.")
    return None


class RankDropdown(discord.ui.Select[discord.ui.View]):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        options = [discord.SelectOption(label=rank) for rank in bot.settings.lol_ranks]
        super().__init__(
            placeholder="Wybierz rangę Solo/Duo",
            max_values=1,
            options=options,
            custom_id="roles:rank:v2",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        settings = self.bot.settings
        await interaction.response.defer(ephemeral=True)
        new_role = find_role(interaction.guild, self.values[0], settings)
        if new_role is None:
            await interaction.followup.send(
                "Nie udało się zmienić roli. Daj znać administracji serwera.",
                ephemeral=True,
            )
            return
        async with _current_roles(interaction.user) as member:
            if member_has_role(member, settings.verified_role_name, settings):
                await interaction.followup.send(
                    "Po weryfikacji bot sam nadaje rolę rangi.", ephemeral=True
                )
                return
            if member_has_role(member, settings.no_lol_role_name, settings):
                await interaction.followup.send(
                    f"Najpierw usuń rolę **{settings.no_lol_role_name}**.", ephemeral=True
                )
                return
            await _swap_rank_role(member, new_role, settings)
            await _update_member_role(self.bot, member)
        await interaction.followup.send(f"Wybrano rangę **{new_role.name}**.", ephemeral=True)


async def _swap_rank_role(
    member: discord.Member, new_role: discord.Role, settings: Settings
) -> None:
    """Add the new rank before removing old ones; on failure restore the previous ranks."""
    removable = [
        role
        for role in member_roles_named(member, settings.lol_ranks, settings)
        if role != new_role
    ]
    added = new_role not in member.roles
    if added:
        await member.add_roles(new_role, reason="Wybór rangi")
    removed: list[discord.Role] = []
    try:
        # One role at a time, so a partial failure knows exactly what to undo.
        for role in removable:
            await member.remove_roles(role, reason="Zmiana wybranej rangi")
            removed.append(role)
    except Exception:
        # Best effort: go back to the previous ranks instead of a mixed state.
        try:
            if removed:
                await member.add_roles(*removed, reason="Cofnięcie nieudanej zmiany rangi")
            if added:
                await member.remove_roles(new_role, reason="Cofnięcie nieudanej zmiany rangi")
        except Exception:
            logger.warning("Could not roll back rank role for Discord user %s", member.id)
        raise


class RankSelect(_RolePanelView):
    def __init__(self, bot: MoonPoroBot) -> None:
        super().__init__(timeout=None)
        self.add_item(RankDropdown(bot))


class ToggleRoleButton(discord.ui.Button[discord.ui.View]):
    def __init__(
        self,
        bot: MoonPoroBot,
        role_name: str,
        category: RoleCategory,
        row: int,
    ) -> None:
        styles = {
            "server": discord.ButtonStyle.red,
            "position": discord.ButtonStyle.green,
            "optional": discord.ButtonStyle.blurple,
        }
        super().__init__(
            label=role_name,
            style=styles[category],
            custom_id=_custom_id(category, role_name),
            row=row,
        )
        self.bot = bot
        self.role_name = role_name
        self.category = category

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        settings = self.bot.settings
        await interaction.response.defer(ephemeral=True)
        role = find_role(interaction.guild, self.role_name, settings)
        if role is None:
            await interaction.followup.send(
                "Nie udało się zmienić roli. Daj znać administracji serwera.",
                ephemeral=True,
            )
            return
        async with _current_roles(interaction.user) as member:
            if self.category in {RoleCategory.SERVER, RoleCategory.POSITION} and member_has_role(
                member, settings.no_lol_role_name, settings
            ):
                await interaction.followup.send(
                    f"Najpierw usuń rolę **{settings.no_lol_role_name}**.", ephemeral=True
                )
                return
            if self.category is RoleCategory.SERVER and member_has_role(
                member, settings.verified_role_name, settings
            ):
                await interaction.followup.send(
                    "Po weryfikacji bot sam ustawia rolę regionu.", ephemeral=True
                )
                return
            if role in member.roles:
                await member.remove_roles(role, reason="Samodzielne usunięcie roli")
                action = "Usunięto"
            else:
                await member.add_roles(role, reason="Samodzielne nadanie roli")
                action = "Dodano"
            await _update_member_role(self.bot, member)
        await interaction.followup.send(f"{action} rolę **{role.name}**.", ephemeral=True)


class RoleButtons(_RolePanelView):
    def __init__(
        self,
        bot: MoonPoroBot,
        roles: list[str],
        category: RoleCategory,
    ) -> None:
        super().__init__(timeout=None)
        for index, role_name in enumerate(roles[:25]):
            self.add_item(ToggleRoleButton(bot, role_name, category, row=index // 5))


class MiscRolesView(_RolePanelView):
    def __init__(self, bot: MoonPoroBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Nie mam konta w LoL",
        style=discord.ButtonStyle.gray,
        custom_id="roles:no-lol:v2",
    )
    async def no_lol(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[MiscRolesView],
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        settings = self.bot.settings
        await interaction.response.defer(ephemeral=True)
        role = find_role(interaction.guild, settings.no_lol_role_name, settings)
        if role is None:
            await interaction.followup.send(
                "Nie udało się zmienić roli. Daj znać administracji serwera.",
                ephemeral=True,
            )
            return
        async with _current_roles(interaction.user) as member:
            if member_has_role(member, settings.verified_role_name, settings):
                await interaction.followup.send(
                    "Najpierw usuń powiązanie z kontem Riot w sekcji „Moje konto”.", ephemeral=True
                )
                return
            league_roles = settings.lol_ranks + settings.lol_servers + settings.lol_positions
            if member_roles_named(member, league_roles, settings):
                await interaction.followup.send(
                    "Najpierw usuń role związane z League of Legends.", ephemeral=True
                )
                return
            if role in member.roles:
                await member.remove_roles(role, reason="Samodzielne usunięcie roli")
                action = "Usunięto"
            else:
                await member.add_roles(role, reason="Samodzielne nadanie roli")
                action = "Dodano"
            await _update_member_role(self.bot, member)
        await interaction.followup.send(f"{action} rolę **{role.name}**.", ephemeral=True)

    @discord.ui.button(
        label="Usuń wszystkie wybrane role",
        style=discord.ButtonStyle.gray,
        custom_id="roles:remove-all:v2",
    )
    async def remove_all(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[MiscRolesView],
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        settings = self.bot.settings
        await interaction.response.defer(ephemeral=True)
        async with _current_roles(interaction.user) as member:
            verified = member_has_role(member, settings.verified_role_name, settings)
            names = set(settings.lol_positions + settings.optional_roles)
            if not verified:
                # The member role stays: it is granted by the bot, not picked here.
                names.update(settings.lol_ranks + settings.lol_servers)
                names.add(settings.no_lol_role_name)
            selected = member_roles_named(member, names, settings)
            if selected:
                await member.remove_roles(*selected, reason="Usunięcie ról konfigurowalnych")
            await _update_member_role(self.bot, member)
        await interaction.followup.send("Usunięto wybrane role.", ephemeral=True)


class RolesCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self.rank_view = RankSelect(bot)
        self.server_view = RoleButtons(bot, bot.settings.lol_servers, RoleCategory.SERVER)
        self.position_view = RoleButtons(bot, bot.settings.lol_positions, RoleCategory.POSITION)
        self.optional_view = RoleButtons(bot, bot.settings.optional_roles, RoleCategory.OPTIONAL)
        self.misc_view = MiscRolesView(bot)
        for view in (
            self.rank_view,
            self.server_view,
            self.position_view,
            self.optional_view,
            self.misc_view,
        ):
            bot.add_view(view)

    async def _publish_role_panels(
        self, interaction: discord.Interaction, *, include_all: bool
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await safe_send(interaction, "Bot nie może wysłać panelu na tym kanale.")
            return

        await interaction.response.send_message("**Ranga Solo/Duo**", view=RankSelect(self.bot))
        panels: list[tuple[str, discord.ui.View]] = [
            (
                "**Region**",
                RoleButtons(self.bot, self.bot.settings.lol_servers, RoleCategory.SERVER),
            )
        ]
        if include_all:
            panels.extend(
                [
                    (
                        "**Pozycje**",
                        RoleButtons(
                            self.bot,
                            self.bot.settings.lol_positions,
                            RoleCategory.POSITION,
                        ),
                    ),
                    (
                        "**Role opcjonalne**",
                        RoleButtons(
                            self.bot,
                            self.bot.settings.optional_roles,
                            RoleCategory.OPTIONAL,
                        ),
                    ),
                ]
            )
        panels.append(("**Pozostałe**", MiscRolesView(self.bot)))
        for content, view in panels:
            await channel.send(content, view=view)

    def _filter_allowed_roles(
        self, roles: tuple[discord.Role | None, ...]
    ) -> tuple[list[discord.Role], list[discord.Role]]:
        provided = [role for role in roles if role is not None]
        allowed = [
            role
            for role in provided
            if any(
                role_is_configured(role, name, self.bot.settings)
                for name in self.bot.settings.allowed_role_names
            )
        ]
        denied = [role for role in provided if role not in allowed]
        return allowed, denied

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="przyznawanie_roli", description="Publikuje panel wyboru ról")
    async def publish_roles(self, interaction: discord.Interaction) -> None:
        await self._publish_role_panels(interaction, include_all=True)

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="start", description="Publikuje skrócony panel startowy")
    async def publish_start(self, interaction: discord.Interaction) -> None:
        await self._publish_role_panels(interaction, include_all=False)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="dr", description="Nadaje do pięciu dozwolonych ról")
    async def add_roles(
        self,
        interaction: discord.Interaction,
        uzytkownik: discord.Member,
        rola1: discord.Role,
        rola2: discord.Role | None = None,
        rola3: discord.Role | None = None,
        rola4: discord.Role | None = None,
        rola5: discord.Role | None = None,
    ) -> None:
        allowed, denied = self._filter_allowed_roles((rola1, rola2, rola3, rola4, rola5))
        await interaction.response.defer()
        async with _current_roles(uzytkownik) as member:
            rank_roles = [
                role
                for role in allowed
                if any(
                    role_is_configured(role, name, self.bot.settings)
                    for name in self.bot.settings.lol_ranks
                )
            ]
            if len({role.id for role in rank_roles}) > 1:
                await interaction.followup.send("Wybierz tylko jedną rangę Solo/Duo.")
                return
            if rank_roles:
                # Same swap as the panel: never leave the member without a rank.
                await _swap_rank_role(member, rank_roles[0], self.bot.settings)
            other_roles = [role for role in allowed if role not in rank_roles]
            if other_roles:
                await member.add_roles(*other_roles, reason=f"Nadanie przez {interaction.user}")
            await _update_member_role(self.bot, member)
        message = f"Dodano: {', '.join(role.mention for role in allowed) or 'brak'}"
        if denied:
            message += f"\nNiedozwolone: {', '.join(role.name for role in denied)}"
        await interaction.followup.send(message)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="ur", description="Usuwa do pięciu dozwolonych ról")
    async def remove_roles(
        self,
        interaction: discord.Interaction,
        uzytkownik: discord.Member,
        rola1: discord.Role,
        rola2: discord.Role | None = None,
        rola3: discord.Role | None = None,
        rola4: discord.Role | None = None,
        rola5: discord.Role | None = None,
    ) -> None:
        configured, _ = self._filter_allowed_roles((rola1, rola2, rola3, rola4, rola5))
        await interaction.response.defer()
        async with _current_roles(uzytkownik) as member:
            allowed = [role for role in configured if role in member.roles]
            if allowed:
                # No member role sync here: removing roles cannot newly qualify
                # anyone, and it would put back an explicitly removed „Użytkownik”.
                await member.remove_roles(*allowed, reason=f"Usunięcie przez {interaction.user}")
        await interaction.followup.send(
            f"Usunięto: {', '.join(role.mention for role in allowed) or 'brak'}."
        )

    def _role_problem(self, member: discord.Member) -> bool:
        settings = self.bot.settings
        ranks = member_roles_named(member, settings.lol_ranks, settings)
        servers = member_roles_named(member, settings.lol_servers, settings)
        has_member = member_has_role(member, settings.member_role_name, settings)
        has_no_lol = member_has_role(member, settings.no_lol_role_name, settings)
        # Having the member role without region or rank is fine: removing a
        # verification keeps it and the bot never takes it away automatically.
        return (
            len(ranks) > 1
            or (
                has_no_lol
                and bool(
                    ranks + servers + member_roles_named(member, settings.lol_positions, settings)
                )
            )
            or (not has_member and _qualifies_for_member_role(member, settings))
        )

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(
        name="pbr", description="Pokazuje osoby z brakującymi albo zbędnymi rolami"
    )
    async def show_broken_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = await _require_guild(interaction)
        if guild is None:
            return
        members = [member.mention for member in guild.members if self._role_problem(member)]
        if not members:
            await interaction.followup.send("Wszystkie role się zgadzają.", ephemeral=True)
            return
        for start in range(0, len(members), 30):
            await interaction.followup.send(", ".join(members[start : start + 30]), ephemeral=True)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="nr", description="Poprawia brakujące albo zbędne role")
    async def repair_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = await _require_guild(interaction)
        if guild is None:
            return
        changed = 0
        for member in guild.members:
            # Avoid REST calls for every member on large guilds; only revisit candidates.
            if not self._role_problem(member):
                continue
            async with _current_roles(member) as current:
                if await _update_member_role(self.bot, current):
                    changed += 1
        await interaction.followup.send(f"Zmienione osoby: {changed}.", ephemeral=True)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="pr", description="Pokazuje niezweryfikowane osoby z daną rolą")
    async def show_unverified_with_role(
        self, interaction: discord.Interaction, rola: discord.Role
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = await _require_guild(interaction)
        if guild is None:
            return
        settings = self.bot.settings
        members = [
            member.mention
            for member in guild.members
            if rola in member.roles
            and not member_has_role(member, settings.verified_role_name, settings)
        ]
        if not members:
            await interaction.followup.send("Nie ma takich osób.", ephemeral=True)
            return
        for start in range(0, len(members), 30):
            await interaction.followup.send(", ".join(members[start : start + 30]), ephemeral=True)

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(
        name="napraw_weryfikacje",
        description="Zdejmuje role weryfikacji, rangi i regionu osobom bez połączonego konta Riot",
    )
    async def repair_verifications(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = await _require_guild(interaction)
        if guild is None:
            return
        settings = self.bot.settings
        links = await self.bot.verifications.list_for_guild(guild.id)
        linked_ids = {link.discord_user_id for link in links}
        verified_role = find_role(guild, settings.verified_role_name, settings)
        fixed = 0
        if verified_role is not None:
            for member in guild.members:
                if verified_role not in member.roles or member.id in linked_ids:
                    continue
                async with _current_roles(member) as current:
                    if verified_role not in current.roles:
                        continue
                    if await self.bot.verifications.get_by_user(guild.id, member.id) is not None:
                        continue
                    to_remove = [
                        *member_roles_named(
                            current, set(settings.lol_ranks + settings.lol_servers), settings
                        ),
                        verified_role,
                    ]
                    await current.remove_roles(*to_remove, reason="Brak rekordu weryfikacji")
                    fixed += 1
        await interaction.followup.send(f"Poprawione osoby: {fixed}.", ephemeral=True)


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(RolesCog(bot))
