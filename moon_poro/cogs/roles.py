from __future__ import annotations

import hashlib
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from moon_poro.bot import MoonPoroBot
from moon_poro.permissions import administrator_only, moderator_only
from moon_poro.roles import (
    find_role,
    member_has_role,
    member_roles_named,
    role_is_configured,
)


def _custom_id(prefix: str, value: str) -> str:
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=8).hexdigest()
    return f"roles:{prefix}:{digest}"


async def _update_member_role(bot: MoonPoroBot, member: discord.Member) -> None:
    settings = bot.settings
    member_role = find_role(member.guild, settings.member_role_name, settings)
    if member_role is None:
        return
    has_required_roles = bool(
        member_roles_named(member, settings.lol_servers, settings)
    ) and bool(
        member_roles_named(member, settings.lol_ranks, settings)
    )
    should_have = has_required_roles or member_has_role(member, settings.no_lol_role_name, settings)
    if should_have and member_role not in member.roles:
        await member.add_roles(member_role, reason="Synchronizacja ról Moon Poro")
    elif not should_have and member_role in member.roles:
        await member.remove_roles(member_role, reason="Synchronizacja ról Moon Poro")


class RankSelect(discord.ui.View):
    def __init__(self, bot: MoonPoroBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        options = [discord.SelectOption(label=rank) for rank in bot.settings.lol_ranks]
        select = discord.ui.Select(
            placeholder="Aktualna dywizja Solo/Duo",
            max_values=1,
            options=options,
            custom_id="roles:rank:v2",
        )
        select.callback = self._select_rank
        self.add_item(select)

    async def _select_rank(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        settings = self.bot.settings
        if member_has_role(interaction.user, settings.verified_role_name, settings):
            await interaction.response.send_message(
                "Zweryfikowane konta otrzymują rangę automatycznie.", ephemeral=True
            )
            return
        if member_has_role(interaction.user, settings.no_lol_role_name, settings):
            await interaction.response.send_message(
                f"Najpierw usuń rolę **{settings.no_lol_role_name}**.", ephemeral=True
            )
            return

        select = self.children[0]
        assert isinstance(select, discord.ui.Select)
        new_role = find_role(interaction.guild, select.values[0], settings)
        if new_role is None:
            await interaction.response.send_message(
                "Skonfigurowana rola nie istnieje.", ephemeral=True
            )
            return
        old_roles = member_roles_named(interaction.user, settings.lol_ranks, settings)
        await interaction.response.defer(ephemeral=True)
        removable = [role for role in old_roles if role != new_role]
        if removable:
            await interaction.user.remove_roles(*removable, reason="Zmiana wybranej rangi")
        if new_role not in interaction.user.roles:
            await interaction.user.add_roles(new_role, reason="Wybór rangi")
        await _update_member_role(self.bot, interaction.user)
        await interaction.followup.send(f"Wybrano rangę **{new_role.name}**.", ephemeral=True)


class ToggleRoleButton(discord.ui.Button):
    def __init__(
        self,
        bot: MoonPoroBot,
        role_name: str,
        category: Literal["server", "position", "optional"],
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
        if self.category in {"server", "position"} and member_has_role(
            interaction.user, settings.no_lol_role_name, settings
        ):
            await interaction.response.send_message(
                f"Najpierw usuń rolę **{settings.no_lol_role_name}**.", ephemeral=True
            )
            return
        if self.category == "server" and member_has_role(
            interaction.user, settings.verified_role_name, settings
        ):
            await interaction.response.send_message(
                "Region zweryfikowanego konta jest zarządzany automatycznie.", ephemeral=True
            )
            return

        role = find_role(interaction.guild, self.role_name, settings)
        if role is None:
            await interaction.response.send_message(
                "Skonfigurowana rola nie istnieje.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="Samodzielne usunięcie roli")
            action = "Usunięto"
        else:
            await interaction.user.add_roles(role, reason="Samodzielne nadanie roli")
            action = "Dodano"
        await _update_member_role(self.bot, interaction.user)
        await interaction.followup.send(f"{action} rolę **{role.name}**.", ephemeral=True)


class RoleButtons(discord.ui.View):
    def __init__(
        self,
        bot: MoonPoroBot,
        roles: list[str],
        category: Literal["server", "position", "optional"],
    ) -> None:
        super().__init__(timeout=None)
        for index, role_name in enumerate(roles[:25]):
            self.add_item(ToggleRoleButton(bot, role_name, category, row=index // 5))


class MiscRolesView(discord.ui.View):
    def __init__(self, bot: MoonPoroBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Nie posiadam konta w LoL",
        style=discord.ButtonStyle.gray,
        custom_id="roles:no-lol:v2",
    )
    async def no_lol(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.guild is None:
            return
        settings = self.bot.settings
        if member_has_role(interaction.user, settings.verified_role_name, settings):
            await interaction.response.send_message(
                "Najpierw usuń weryfikację konta Riot.", ephemeral=True
            )
            return
        league_roles = settings.lol_ranks + settings.lol_servers + settings.lol_positions
        if member_roles_named(interaction.user, league_roles, settings):
            await interaction.response.send_message(
                "Najpierw usuń role związane z League of Legends.", ephemeral=True
            )
            return
        role = find_role(interaction.guild, settings.no_lol_role_name, settings)
        if role is None:
            await interaction.response.send_message(
                "Skonfigurowana rola nie istnieje.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="Samodzielne usunięcie roli")
            action = "Usunięto"
        else:
            await interaction.user.add_roles(role, reason="Samodzielne nadanie roli")
            action = "Dodano"
        await _update_member_role(self.bot, interaction.user)
        await interaction.followup.send(f"{action} rolę **{role.name}**.", ephemeral=True)

    @discord.ui.button(
        label="Usuń wszystkie role konfigurowalne",
        style=discord.ButtonStyle.gray,
        custom_id="roles:remove-all:v2",
    )
    async def remove_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        settings = self.bot.settings
        verified = member_has_role(interaction.user, settings.verified_role_name, settings)
        names = set(settings.lol_positions + settings.optional_roles)
        if not verified:
            names.update(settings.lol_ranks + settings.lol_servers)
            names.update([settings.member_role_name, settings.no_lol_role_name])
        roles = [
            role
            for role in interaction.user.roles
            if any(role_is_configured(role, name, settings) for name in names)
        ]
        await interaction.response.defer(ephemeral=True)
        if roles:
            await interaction.user.remove_roles(*roles, reason="Usunięcie ról konfigurowalnych")
        await _update_member_role(self.bot, interaction.user)
        await interaction.followup.send("Usunięto wybrane role.", ephemeral=True)


class RolesCog(commands.Cog):
    def __init__(self, bot: MoonPoroBot) -> None:
        self.bot = bot
        self.rank_view = RankSelect(bot)
        self.server_view = RoleButtons(bot, bot.settings.lol_servers, "server")
        self.position_view = RoleButtons(bot, bot.settings.lol_positions, "position")
        self.optional_view = RoleButtons(bot, bot.settings.optional_roles, "optional")
        self.misc_view = MiscRolesView(bot)
        for view in (
            self.rank_view,
            self.server_view,
            self.position_view,
            self.optional_view,
            self.misc_view,
        ):
            bot.add_view(view)

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="przyznawanie_roli", description="Publikuje panel wyboru ról")
    async def publish_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("**Ranga Solo/Duo**", view=RankSelect(self.bot))
        await interaction.channel.send(
            "**Region**", view=RoleButtons(self.bot, self.bot.settings.lol_servers, "server")
        )
        await interaction.channel.send(
            "**Pozycje**", view=RoleButtons(self.bot, self.bot.settings.lol_positions, "position")
        )
        await interaction.channel.send(
            "**Role opcjonalne**",
            view=RoleButtons(self.bot, self.bot.settings.optional_roles, "optional"),
        )
        await interaction.channel.send("**Pozostałe**", view=MiscRolesView(self.bot))

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="start", description="Publikuje skrócony panel startowy")
    async def publish_start(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("**Ranga Solo/Duo**", view=RankSelect(self.bot))
        await interaction.channel.send(
            "**Region**", view=RoleButtons(self.bot, self.bot.settings.lol_servers, "server")
        )
        await interaction.channel.send("**Pozostałe**", view=MiscRolesView(self.bot))

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
        roles = [role for role in (rola1, rola2, rola3, rola4, rola5) if role]
        allowed = [
            role
            for role in roles
            if any(
                role_is_configured(role, name, self.bot.settings)
                for name in self.bot.settings.allowed_role_names
            )
        ]
        denied = [role for role in roles if role not in allowed]
        await interaction.response.defer()
        if allowed:
            rank_roles = [
                role
                for role in allowed
                if any(
                    role_is_configured(role, name, self.bot.settings)
                    for name in self.bot.settings.lol_ranks
                )
            ]
            if rank_roles:
                existing = member_roles_named(
                    uzytkownik, self.bot.settings.lol_ranks, self.bot.settings
                )
                if existing:
                    await uzytkownik.remove_roles(
                        *existing, reason=f"Zmiana przez {interaction.user}"
                    )
            await uzytkownik.add_roles(*allowed, reason=f"Nadanie przez {interaction.user}")
        await _update_member_role(self.bot, uzytkownik)
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
        roles = [role for role in (rola1, rola2, rola3, rola4, rola5) if role]
        allowed = [
            role
            for role in roles
            if role in uzytkownik.roles
            and any(
                role_is_configured(role, name, self.bot.settings)
                for name in self.bot.settings.allowed_role_names
            )
        ]
        await interaction.response.defer()
        if allowed:
            await uzytkownik.remove_roles(*allowed, reason=f"Usunięcie przez {interaction.user}")
        await _update_member_role(self.bot, uzytkownik)
        await interaction.followup.send(
            f"Usunięto: {', '.join(role.mention for role in allowed) or 'brak'}."
        )

    def _role_problem(self, member: discord.Member) -> bool:
        settings = self.bot.settings
        ranks = member_roles_named(member, settings.lol_ranks, settings)
        servers = member_roles_named(member, settings.lol_servers, settings)
        has_member = member_has_role(member, settings.member_role_name, settings)
        has_no_lol = member_has_role(member, settings.no_lol_role_name, settings)
        return (
            len(ranks) > 1
            or (
                has_no_lol
                and bool(
                    ranks
                    + servers
                    + member_roles_named(member, settings.lol_positions, settings)
                )
            )
            or (has_member != (has_no_lol or (bool(ranks) and bool(servers))))
        )

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="pbr", description="Pokazuje osoby z niespójnymi rolami")
    async def show_broken_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        members = [
            member.mention for member in interaction.guild.members if self._role_problem(member)
        ]
        if not members:
            await interaction.followup.send("Nie znaleziono niespójnych ról.", ephemeral=True)
            return
        for start in range(0, len(members), 30):
            await interaction.followup.send(", ".join(members[start : start + 30]), ephemeral=True)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="nr", description="Naprawia podstawowe zależności ról")
    async def repair_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        changed = 0
        for member in interaction.guild.members:
            before = set(member.roles)
            await _update_member_role(self.bot, member)
            if set(member.roles) != before:
                changed += 1
        await interaction.followup.send(f"Zaktualizowano {changed} użytkowników.", ephemeral=True)

    @moderator_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.command(name="pr", description="Pokazuje niezweryfikowane osoby z daną rolą")
    async def show_unverified_with_role(
        self, interaction: discord.Interaction, rola: discord.Role
    ) -> None:
        settings = self.bot.settings
        members = [
            member.mention
            for member in interaction.guild.members
            if rola in member.roles
            and not member_has_role(member, settings.verified_role_name, settings)
        ]
        await interaction.response.send_message(
            ", ".join(members) if members else "Nie znaleziono użytkowników.", ephemeral=True
        )

    @administrator_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(
        name="napraw_weryfikacje", description="Usuwa role weryfikacji bez rekordu w bazie"
    )
    async def repair_verifications(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        settings = self.bot.settings
        links = await self.bot.verifications.list_for_guild(interaction.guild_id or 0)
        linked_ids = {link.discord_user_id for link in links}
        verified_role = find_role(interaction.guild, settings.verified_role_name, settings)
        fixed = 0
        if verified_role is not None:
            for member in interaction.guild.members:
                if verified_role not in member.roles or member.id in linked_ids:
                    continue
                to_remove = [
                    *member_roles_named(
                        member, set(settings.lol_ranks + settings.lol_servers), settings
                    ),
                    verified_role,
                ]
                await member.remove_roles(*to_remove, reason="Brak rekordu weryfikacji")
                fixed += 1
        await interaction.followup.send(f"Naprawiono {fixed} użytkowników.", ephemeral=True)


async def setup(bot: MoonPoroBot) -> None:
    await bot.add_cog(RolesCog(bot))
