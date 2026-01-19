import discord
import config


def has_rank_roles(member: discord.Member) -> bool:
    member_role_names = {role.name for role in member.roles}
    return bool(member_role_names.intersection(config.lol_ranks))


def has_server_roles(member: discord.Member) -> bool:
    member_role_names = {role.name for role in member.roles}
    return bool(member_role_names.intersection(config.lol_servers))


def has_other_roles(member: discord.Member) -> bool:
    member_role_names = {role.name for role in member.roles}
    return bool(member_role_names.intersection(config.lol_other))


def get_member_rank_roles(member: discord.Member) -> list[discord.Role]:
    return [role for role in member.roles if role.name in config.lol_ranks]


def get_member_server_roles(member: discord.Member) -> list[discord.Role]:
    return [role for role in member.roles if role.name in config.lol_servers]