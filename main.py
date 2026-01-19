import datetime
import logging
import logging.handlers

import asyncpg
import discord
from discord.ext import commands
from pulsefire.clients import RiotAPIClient

import config
from utils.errors import setup_error_handler


class Bot(commands.Bot):
    def __init__(self, intents: discord.Intents):
        super().__init__(
            command_prefix='%',
            intents=intents
        )
        self.join_check = True
        self.pool: asyncpg.Pool = None
        self.riot_client: RiotAPIClient = None

    async def setup_hook(self) -> None:
        extensions = [
            'przyznawanie_roli', 'warn', 'role', 'ticket', 
            'weryfikacja', 'sponsors', 'mod_stats', 'beebo'
        ]
        for ext in extensions:
            await self.load_extension(f"cogs.{ext}")
        
        setup_error_handler(self)
        await self.tree.sync(guild=discord.Object(id=config.guild_id))

    async def on_ready(self) -> None:
        logger.info(f"Zalogowano jako {self.user}!")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
            
        content_lower = message.content.lower()
        
        if "buu" in content_lower:
            await message.channel.send("Waaa")
        
        if "jd" in content_lower:
            jd = discord.utils.get(message.guild.roles, name="JD")
            if jd:
                await message.author.add_roles(jd)
        
        if message.channel.id == config.szukanie_gry_channel_id and 'clash' in content_lower:
            await message.delete()
            try:
                await message.author.send(
                    "Na kanale #szukanie-gry obowiązuje zakaz szukania na clash. "
                    "Przenieś się na kanał #clash. Próby ominięcia tego zakazu "
                    "zakończą się dwoma warnami."
                )
            except discord.errors.Forbidden:
                pass

    async def on_member_join(self, member: discord.Member) -> None:
        if not self.join_check:
            return
            
        account_age = datetime.datetime.now(datetime.timezone.utc) - member.created_at
        if account_age < datetime.timedelta(days=90):
            await member.ban(reason="Multikonto - konto młodsze niż 90 dni")
            channel = member.guild.get_channel(config.komendy_botowe_channel_id)
            if channel:
                await channel.send(f"🔨 Zbanowano {member.mention} za multikonto!")

    async def on_member_remove(self, member: discord.Member) -> None:
        channel = member.guild.get_channel(config.komendy_botowe_channel_id)
        if channel:
            await channel.send(f"👋 {member.mention} wyszedł z serwera.")

    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        channel = guild.get_channel(config.komendy_botowe_channel_id)
        if channel:
            await channel.send(f"🔨 {user.mention} został zbanowany.")


logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)

file_handler = logging.handlers.RotatingFileHandler(
    filename='discord.log',
    encoding='utf-8',
    maxBytes=32 * 1024 * 1024,
    backupCount=3
)
console_handler = logging.StreamHandler()

formatter = logging.Formatter(
    '[{asctime}] [{levelname:<8}] {name}: {message}',
    datefmt='%Y-%m-%d %H:%M:%S',
    style='{'
)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)


def create_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.message_content = True
    intents.guild_messages = True
    intents.messages = True
    intents.emojis = True
    intents.members = True
    intents.guilds = True
    intents.moderation = True
    return intents


async def main() -> None:
    intents = create_intents()
    bot = Bot(intents)
    
    async with (
        bot,
        asyncpg.create_pool(**config.POSTGRES_INFO) as pool,
        RiotAPIClient(default_headers={"X-Riot-Token": config.riot_api_token}) as riot_client
    ):
        bot.pool = pool
        bot.riot_client = riot_client
        await bot.start(config.token)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
