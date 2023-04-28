from discord.ext import commands
from discord import Message
from discord import Object
from discord import File
import datetime
import random
import config
import os


class Beebo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cd = datetime.datetime(2023, 4, 27)

    @commands.Cog.listener('on_message')
    async def on_message(self, message: Message):
        if "poka Beebo" in message.content:
            time = datetime.datetime.now()
            delay = time - self.cd
            if delay.total_seconds() < 600:
                await message.channel.send("Spokojnie! Beebo śpi... poczekaj parę minut!  =＾´• ⋏ •`＾=")
                return
            
            file = File("img/" + random.choice(os.listdir("img")))
            await message.channel.send(file=file)
            self.cd = time

async def setup(bot: commands.Bot):
    await bot.add_cog(Beebo(bot), guild = Object(id = config.guild_id))