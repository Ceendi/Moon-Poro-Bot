import discord

SERVER_TRANSLATION = {
    'EUN1': 'EUNE',
    'EUW1': 'EUW',
    'NA1': 'NA'
}

SERVERS = {
    'eune': 'EUN1',
    'euw': 'EUW1',
    'na': 'NA1'
}

API_SERVERS = {
    'eune': 'europe',
    'euw': 'europe',
    'na': 'americas',
    'EUN1': 'europe',
    'EUW1': 'europe',
    'NA1': 'americas'
}

RANK_TO_ROLE = {
    'IRON': 'Iron',
    'BRONZE': 'Bronze',
    'SILVER': 'Silver',
    'GOLD': 'Gold',
    'PLATINUM': 'Platinum',
    'EMERALD': 'Emerald',
    'DIAMOND': 'Diamond',
    'MASTER': 'Master',
    'GRANDMASTER': 'GrandMaster',
    'CHALLENGER': 'Challenger',
    'UNRANKED': 'Unranked'
}

ROLE_ZWERYFIKOWANY = "Zweryfikowany"
ROLE_UZYTKOWNIK = "Użytkownik"
ROLE_NPKWL = "Nie posiadam konta w lolu"
ROLE_UNRANKED = "Unranked"

COLOR_SUCCESS = discord.Colour.green()
COLOR_ERROR = discord.Colour.red()
COLOR_WARNING = discord.Colour.orange()
COLOR_INFO = discord.Colour.blue()
COLOR_EXPIRED = discord.Colour.dark_gray()


def get_profile_icon_url(icon_id: int) -> str:
    return f'https://raw.communitydragon.org/12.13/game/assets/ux/summonericons/profileicon{icon_id}.png'


BOOST_KEYWORDS = [
    'boost', 'wbije rangę', 'wbije range', 'bost', 
    'pomogę z', 'pomoge z', 'za free', 'tanio'
]
