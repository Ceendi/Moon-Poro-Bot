import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from moon_poro.cogs.roles import (
    MiscRolesView,
    RankDropdown,
    RoleCategory,
    RolesCog,
    ToggleRoleButton,
)
from moon_poro.member_role_lock import _entries, member_role_lock


class DiscordRoles:
    """Real discord.Member objects whose HTTP writes leave their snapshots unchanged."""

    names = ("@everyone", "Gold", "Silver", "EUNE", "Member", "No LoL", "Verified", "Mid", "TFT")

    def __init__(self, *initial: str) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        self.state = self.client._connection
        self.guild = discord.Guild(
            state=self.state,
            data={
                "id": "1",
                "name": "Test",
                "owner_id": "99",
                "roles": [
                    {"id": str(i + 1), "name": name, "position": i}
                    for i, name in enumerate(self.names)
                ],
            },
        )
        self.roles = {role.name: role for role in self.guild.roles}
        self.actual = {self.roles[name].id for name in initial}
        self.http = SimpleNamespace(
            add_role=AsyncMock(side_effect=self.add),
            remove_role=AsyncMock(side_effect=self.remove),
            get_member=AsyncMock(side_effect=self.fetch),
        )
        self.state.http = self.http
        self.bot = SimpleNamespace(
            settings=SimpleNamespace(
                member_role_name="Member",
                no_lol_role_name="No LoL",
                verified_role_name="Verified",
                lol_servers=["EUNE"],
                lol_ranks=["Gold", "Silver"],
                lol_positions=["Mid"],
                optional_roles=["TFT"],
                role_ids={},
                allowed_role_names=frozenset(self.names[1:]),
            )
        )
        self.member = self.snapshot()
        self.guild._members[self.member.id] = self.member

    def payload(self):
        return {
            "user": {"id": "42", "username": "test", "discriminator": "0", "avatar": None},
            "roles": list(map(str, self.actual)),
            "flags": 0,
        }

    def snapshot(self):
        return discord.Member(data=self.payload(), guild=self.guild, state=self.state)

    async def fetch(self, *_args):
        await asyncio.sleep(0)
        return self.payload()

    async def add(self, _guild_id, _user_id, role_id, **_kwargs):
        await asyncio.sleep(0)
        self.actual.add(role_id)

    async def remove(self, _guild_id, _user_id, role_id, **_kwargs):
        await asyncio.sleep(0)
        self.actual.discard(role_id)

    def interaction(self):
        return SimpleNamespace(
            user=self.member,
            guild=self.guild,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def selected(self):
        return {name for name, role in self.roles.items() if role.id in self.actual}

    def cog(self):
        cog = object.__new__(RolesCog)
        cog.bot = self.bot
        return cog


@pytest.mark.parametrize(
    "initial,expected",
    [
        (("Gold",), {"Gold", "EUNE", "Member"}),
        # The member role is never taken away automatically.
        (("Gold", "EUNE", "Member"), {"Gold", "Member"}),
    ],
)
async def test_toggle_region_uses_actual_http_result(initial, expected):
    env = DiscordRoles(*initial)
    await ToggleRoleButton(env.bot, "EUNE", RoleCategory.SERVER, 0).callback(env.interaction())
    assert env.selected() == expected
    assert {role.name for role in env.member.roles} == {"@everyone", *initial}


@pytest.mark.parametrize(
    "initial,expected",
    [
        ((), {"No LoL", "Member"}),
        (("No LoL", "Member"), {"Member"}),
    ],
)
async def test_no_lol_updates_member_from_http_result(initial, expected):
    env = DiscordRoles(*initial)
    view = MiscRolesView(env.bot)
    await view.no_lol.callback(env.interaction())
    assert env.selected() == expected


@pytest.mark.parametrize(
    "initial,expected",
    [
        (("Gold", "EUNE", "Member", "TFT"), {"Member"}),
        (
            ("Gold", "EUNE", "Member", "Verified", "Mid", "TFT"),
            {"Gold", "EUNE", "Member", "Verified"},
        ),
    ],
)
async def test_remove_all_preserves_only_protected_verified_roles(initial, expected):
    env = DiscordRoles(*initial)
    await MiscRolesView(env.bot).remove_all.callback(env.interaction())
    assert env.selected() == expected


async def test_failed_rank_add_keeps_old_rank():
    env = DiscordRoles("Gold", "EUNE", "Member")
    env.http.add_role.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions"
    )
    dropdown = RankDropdown(env.bot)
    dropdown._values = ["Silver"]
    with pytest.raises(discord.Forbidden):
        await dropdown.callback(env.interaction())
    assert env.selected() == {"Gold", "EUNE", "Member"}


async def test_rank_change_removes_old_rank_and_keeps_member():
    env = DiscordRoles("Gold", "EUNE", "Member")
    dropdown = RankDropdown(env.bot)
    dropdown._values = ["Silver"]
    await dropdown.callback(env.interaction())
    assert env.selected() == {"Silver", "EUNE", "Member"}


async def test_concurrent_rank_changes_and_region_toggle_use_fresh_state():
    env = DiscordRoles("EUNE")
    choices = [RankDropdown(env.bot), RankDropdown(env.bot)]
    choices[0]._values = ["Gold"]
    choices[1]._values = ["Silver"]
    await asyncio.gather(*(choice.callback(env.interaction()) for choice in choices))
    assert env.selected() == {"Silver", "EUNE", "Member"}
    button = ToggleRoleButton(env.bot, "EUNE", RoleCategory.SERVER, 0)
    await asyncio.gather(button.callback(env.interaction()), button.callback(env.interaction()))
    assert env.selected() == {"Silver", "EUNE", "Member"}
    assert not _entries


async def test_concurrent_no_lol_and_rank_do_not_mix_exclusive_roles():
    env = DiscordRoles()
    rank = RankDropdown(env.bot)
    rank._values = ["Gold"]
    await asyncio.gather(
        MiscRolesView(env.bot).no_lol.callback(env.interaction()),
        rank.callback(env.interaction()),
    )
    assert env.selected() == {"No LoL", "Member"}


async def test_concurrent_remove_all_and_rank_keep_member_role():
    env = DiscordRoles("Gold", "EUNE", "Member")
    rank = RankDropdown(env.bot)
    rank._values = ["Silver"]
    await asyncio.gather(
        MiscRolesView(env.bot).remove_all.callback(env.interaction()),
        rank.callback(env.interaction()),
    )
    assert env.selected() == {"Silver", "Member"}


@pytest.mark.parametrize("command", ["rank", "region", "no_lol", "remove_all"])
async def test_verified_checks_run_after_waiting_for_role_lock(command):
    env = DiscordRoles("Gold", "EUNE", "Member")
    interaction = env.interaction()
    if command == "rank":
        control = RankDropdown(env.bot)
        control._values = ["Silver"]
        callback = control.callback
    elif command == "region":
        callback = ToggleRoleButton(env.bot, "EUNE", RoleCategory.SERVER, 0).callback
    else:
        callback = getattr(MiscRolesView(env.bot), command).callback
    async with member_role_lock(env.guild.id, env.member.id):
        task = asyncio.create_task(callback(interaction))
        await asyncio.sleep(0)
        env.actual.add(env.roles["Verified"].id)
    await task
    assert env.selected() == {"Gold", "EUNE", "Member", "Verified"}
    assert not _entries


async def test_moderator_add_remove_and_multi_rank_keep_dependencies():
    env = DiscordRoles("Gold")
    cog = env.cog()
    await RolesCog.add_roles.callback(cog, env.interaction(), env.member, env.roles["EUNE"])
    assert env.selected() == {"Gold", "EUNE", "Member"}
    await RolesCog.remove_roles.callback(cog, env.interaction(), env.member, env.roles["EUNE"])
    assert env.selected() == {"Gold", "Member"}
    await RolesCog.add_roles.callback(
        cog,
        env.interaction(),
        env.member,
        env.roles["Gold"],
        env.roles["Silver"],
        env.roles["EUNE"],
    )
    assert env.selected() == {"Gold", "Member"}
    await RolesCog.add_roles.callback(cog, env.interaction(), env.member, env.roles["Silver"])
    assert env.selected() == {"Silver", "Member"}
    # A moderator can still take the member role away explicitly.
    await RolesCog.remove_roles.callback(cog, env.interaction(), env.member, env.roles["Member"])
    assert env.selected() == {"Silver"}


async def test_moderator_can_remove_member_role_from_a_qualifying_member():
    env = DiscordRoles("Gold", "EUNE", "Member")
    cog = env.cog()
    await RolesCog.remove_roles.callback(cog, env.interaction(), env.member, env.roles["Member"])
    assert env.selected() == {"Gold", "EUNE"}


async def test_moderator_rank_change_adds_the_new_rank_before_removing_the_old_one():
    env = DiscordRoles("Gold", "EUNE", "Member")
    cog = env.cog()
    await RolesCog.add_roles.callback(cog, env.interaction(), env.member, env.roles["Silver"])
    assert env.selected() == {"Silver", "EUNE", "Member"}


async def test_repair_counts_http_changes_and_skips_consistent_members():
    env = DiscordRoles("Gold", "EUNE")
    cog = env.cog()
    interaction = env.interaction()
    await RolesCog.repair_roles.callback(cog, interaction)
    assert env.selected() == {"Gold", "EUNE", "Member"}
    assert interaction.followup.send.await_args.args[0] == "Zmienione osoby: 1."
    env.guild._members[42] = env.snapshot()
    await RolesCog.repair_roles.callback(cog, env.interaction())
    assert env.http.get_member.await_count == 1


async def test_large_unverified_list_is_deferred_and_chunked():
    env = DiscordRoles("TFT")
    members = [
        SimpleNamespace(mention=f"<@{10**18 + i}>", roles=[env.roles["TFT"]]) for i in range(50_000)
    ]

    class Guild:
        @property
        def members(self):
            assert interaction.response.defer.await_count == 1
            return members

    interaction = env.interaction()
    interaction.guild = Guild()
    await RolesCog.show_unverified_with_role.callback(env.cog(), interaction, env.roles["TFT"])
    messages = [call.args[0] for call in interaction.followup.send.await_args_list]
    assert all(len(message) <= 2000 for message in messages)
    assert ", ".join(messages) == ", ".join(member.mention for member in members)


async def test_verification_repair_rechecks_link_created_after_initial_scan():
    env = DiscordRoles("Gold", "EUNE", "Member", "Verified")
    env.bot.verifications = SimpleNamespace(
        list_for_guild=AsyncMock(return_value=[]),
        get_by_user=AsyncMock(return_value=SimpleNamespace(discord_user_id=42)),
    )
    interaction = env.interaction()
    await RolesCog.repair_verifications.callback(env.cog(), interaction)
    assert env.selected() == {"Gold", "EUNE", "Member", "Verified"}
    assert not env.http.remove_role.await_count
    assert interaction.followup.send.await_args.args[0] == "Poprawione osoby: 0."


async def test_verification_repair_preserves_roles_after_self_delete():
    env = DiscordRoles("Gold", "EUNE", "Member", "Verified")
    env.bot.verifications = SimpleNamespace(
        list_for_guild=AsyncMock(return_value=[]), get_by_user=AsyncMock(return_value=None)
    )
    async with member_role_lock(env.guild.id, env.member.id):
        task = asyncio.create_task(
            RolesCog.repair_verifications.callback(env.cog(), env.interaction())
        )
        await asyncio.sleep(0)
        env.actual.discard(env.roles["Verified"].id)
    await task
    assert env.selected() == {"Gold", "EUNE", "Member"}
    assert not env.http.remove_role.await_count


async def test_verification_repair_removes_only_unlinked_verified_candidates():
    env = DiscordRoles("Gold", "EUNE", "Member", "Verified")
    env.bot.verifications = SimpleNamespace(
        list_for_guild=AsyncMock(return_value=[]), get_by_user=AsyncMock(return_value=None)
    )
    await RolesCog.repair_verifications.callback(env.cog(), env.interaction())
    assert env.selected() == {"Member"}
    env.guild._members[42] = env.snapshot()
    await RolesCog.repair_verifications.callback(env.cog(), env.interaction())
    assert env.http.get_member.await_count == 1


async def test_cancelled_lock_waiter_releases_entry():
    async with member_role_lock(1, 42):

        async def wait():
            async with member_role_lock(1, 42):
                pytest.fail("Cancelled waiter entered")

        task = asyncio.create_task(wait())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not _entries
