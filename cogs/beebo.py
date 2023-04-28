from discord.ext import commands
from discord import Message
from discord import Object
from discord import File
import random
import config
import os

def key(message: Message):
    return message.author

class Beebo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cd = commands.CooldownMapping.from_cooldown(1.0, 300.0, key)

    @commands.Cog.listener('on_message')
    async def on_message(self, message: Message):
        if "poka Beebo" in message.content:
            retry_after = self.cd.update_rate_limit(message)
            if retry_after:
                await message.channel.send("Spokojnie! Beebo śpi... poczekaj parę minut!  =＾´• ⋏ •`＾=")
                return
            
            rand = random.randrange(1,131)
            file = File("img/" + random.choice(os.listdir("img")))
            await message.channel.send(file=file)

async def setup(bot: commands.Bot):
    await bot.add_cog(Beebo(bot), guild = Object(id = config.guild_id))