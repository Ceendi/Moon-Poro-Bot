from datetime import datetime
import discord
from discord.ext import commands
import config
import asyncpg
import asyncio
import datetime
import logging.handlers


class Bot(commands.Bot):
    def __init__(self, intents):
        super().__init__(
            command_prefix='%',
            intents=intents
        )
        self.join_check = True

    async def setup_hook(self):
        extensions = ['przyznawanie_roli', 'warn', 'role', 'ticket', 'weryfikacja', 'sponsors', 'mod_stats', 'beebo']
        for ext in extensions:
            await self.load_extension(f"cogs.{ext}")
        await self.tree.sync(guild=discord.Object(id=config.guild_id))

    async def on_ready(self):
        print(f"Zalogowano jako {self.user}!")

    async def on_message(self, message: discord.Message):
        if "buu" in message.content.lower():
            await message.channel.send("Waaa")
        if "jd" in message.content.lower():
            jd = discord.utils.get(message.guild.roles, name="JD")
            await message.author.add_roles(jd)
        if message.channel.id == config.szukanie_gry_channel_id and 'clash' in message.content.lower():
            await message.delete()
            try:
                await message.author.send(
                    "Na kanale #szukanie-gry obowiązuje zakaz szukania na clash. Przenieś się na kanał #clash. Próby "
                    "ominięcia tego zakazu zakończą się dwoma warnami.")
            except discord.errors.Forbidden:
                pass

    async def on_member_join(self, member: discord.Member):
        if member.created_at + datetime.timedelta(days=90) > datetime.datetime.now(
                datetime.timezone.utc) and self.join_check:
            await member.ban(reason="Multikonto")
            channel = member.guild.get_channel(config.komendy_botowe_channel_id)
            await channel.send(f"Zbanowano {member.mention} za multikonto!")
        
    async def on_member_remove(self, member: discord.Member):
        channel = member.guild.get_channel(config.komendy_botowe_channel_id)
        await channel.send(f"{member.mention} wyszedł!")

    async def on_member_ban(self, guild: discord.Guild, member: discord.Member):
        channel = member.guild.get_channel(config.komendy_botowe_channel_id)
        await channel.send(f"{member.mention} zbanowany!")

    async def on_remove_member(self, member: discord.Member):
        channel = member.guild.get_channel(config.komendy_botowe_channel_id)
        await channel.send(f"{member.mention} wyszedł!")

    async def on_member_ban(self, guild: discord.Guild, member: discord.Member):
        channel = member.guild.get_channel(config.komendy_botowe_channel_id)
        await channel.send(f"{member.mention} zbanowany!")


"""ERROR HANDLER"""
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
file_handler = logging.handlers.RotatingFileHandler(filename='discord.log', encoding='utf-8', maxBytes=32 * 1024 * 1024,
                                                    backupCount=3)
console_handler = logging.StreamHandler()
dt_fmt = '%Y-%m-%d %H:%M:%S'
formatter = logging.Formatter('[{asctime}] [{levelname:<8}] {name}: {message}', dt_fmt, style='{')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.addHandler(file_handler)
"""ERROR HANDLER"""

intents = discord.Intents().none()
intents.message_content = True
intents.guild_messages = True
intents.messages = True
intents.emojis = True
intents.members = True
intents.guilds = True
intents.moderation = True
bot = Bot(intents)


async def main():
    async with bot, asyncpg.create_pool(**config.POSTGRES_INFO) as pool:
        bot.pool = pool
        await bot.start(config.token)


asyncio.run(main())
