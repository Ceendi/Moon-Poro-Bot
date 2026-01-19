import os
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("DISCORD_TOKEN")
riot_api_token = os.getenv("RIOT_API_TOKEN")

POSTGRES_INFO = {
    'user': os.getenv("POSTGRES_USER"),
    'password': os.getenv("POSTGRES_PASSWORD"),
    'host': os.getenv("POSTGRES_HOST"),
    'database': os.getenv("POSTGRES_DB")
}

guild_id = int(os.getenv("GUILD_ID", "0"))

warn_channel_id = int(os.getenv("WARN_CHANNEL_ID", "0"))
ticket_channel_id = int(os.getenv("TICKET_CHANNEL_ID", "0"))
zweryfikowani_channel_id = int(os.getenv("ZWERYFIKOWANI_CHANNEL_ID", "0"))
szukanie_gry_channel_id = int(os.getenv("SZUKANIE_GRY_CHANNEL_ID", "0"))
komendy_botowe_channel_id = int(os.getenv("KOMENDY_BOTOWE_CHANNEL_ID", "0"))
mod_alert_channel_id = int(os.getenv("MOD_ALERT_CHANNEL_ID", "0"))
proxy_vc_channel_id = int(os.getenv("PROXY_VC_CHANNEL_ID", "0"))
drzez_vc_channel_id = int(os.getenv("DRZEZ_VC_CHANNEL_ID", "0"))
proxy_log_channel_id = int(os.getenv("PROXY_LOG_CHANNEL_ID", "0"))
general_channel_id = int(os.getenv("GENERAL_CHANNEL_ID", "0"))

drzez_user_id = int(os.getenv("DRZEZ_USER_ID", "0"))

lol_servers = ['EUNE', 'EUW', 'NA']
lol_ranks = ['Unranked', 'Iron', 'Bronze', 'Silver', 'Gold', 'Platinum', 'Emerald', 'Diamond', 'Master', 'GrandMaster', 'Challenger']
lol_other = ['Top', 'Jungle', 'Mid', 'ADC', 'Support', 'Szukam Gry']

ALLOWED_ROLES = lol_other + lol_ranks + lol_servers + [
    "Użytkownik", "Nie posiadam konta w lolu", "Valorant", "LOR", "TFT", 
    "Wild Rift", "Ogłoszenia", "Lol Newsy"
]

OPTIONAL_ROLES = ["TFT", "LOR", "Valorant", "Dyskusje", "Lol Newsy", "Ogłoszenia", "Wild Rift"]

warns = {1: "Warn", 2: "Warn 2", 3: "TIMEOUT"}
warn_days = {"Warn": 7, "Warn 2": 14, "TIMEOUT": 3}

VERIFICATION_TIMEOUT = 120
VERIFICATION_COOLDOWN = 30
VIEW_TIMEOUT = 180