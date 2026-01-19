import datetime
import os
import random

import discord
from discord.ext import commands

import config


class BeeboCog(commands.Cog):
    COOLDOWN_SECONDS = 600

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.last_shown: datetime.datetime = datetime.datetime.min

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if "poka Beebo" not in message.content:
            return
        now = datetime.datetime.now()
        if (now - self.last_shown).total_seconds() < self.COOLDOWN_SECONDS:
            await message.channel.send("😴 Spokojnie! Beebo śpi... poczekaj parę minut! =＾´• ⋏ •`＾=")
            return
        try:
            images = os.listdir("img")
            if not images:
                return
            random_image = random.choice(images)
            file = discord.File(f"img/{random_image}")
            await message.channel.send(file=file)
            self.last_shown = now
        except FileNotFoundError:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(BeeboCog(bot), guild=discord.Object(id=config.guild_id))