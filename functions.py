import discord
import config

def has_rank_roles(member: discord.Member):
    for role in member.roles:
        for rank in config.lol_ranks:
            if str(role) == rank:
                return True
    return False

def has_server_roles(member: discord.Member):
    for role in member.roles:
        for rank in config.lol_servers:
            if str(role) == rank:
                return True
    return False

def has_other_roles(member: discord.Member):
    for role in member.roles:
        for rank in config.lol_other:
            if str(role) == rank:
                return True
    return False