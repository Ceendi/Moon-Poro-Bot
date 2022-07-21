from datetime import datetime
import discord
from discord.ext import commands
import config
import asyncpg
import asyncio
import datetime
import logging
import logging.handlers
from cogs.weryfikacja import Weryfikacja


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix='%',
            intents = discord.Intents.all()
            )

    async def setup_hook(self):
        extensions = ['przyznawanie_roli', 'warn', 'role', 'ticket', 'weryfikacja']
        for ext in extensions:
            await self.load_extension(f"cogs.{ext}")
        await self.tree.sync(guild = discord.Object(id = config.guild_id))
    
    async def on_ready(self):
        print(f"Zalogowano jako {self.user}!")

    async def on_message(self, message: discord.Message):
        if '@everyone' in message.content.lower() and "Administracja" not in str(message.author.roles) and "Moderacja" not in str(message.author.roles):
            data = await self.pool.fetch("SELECT * FROM warn WHERE id=$1;", message.author.id)
            timeout = discord.utils.get(message.guild.roles, name="TIMEOUT")
            now = datetime.datetime.utcnow().replace(microsecond=0)
            enddate = now + datetime.timedelta(days=5)
            warn_channel = message.guild.get_channel(config.warn_channel_id)
            if not data:
                embed = discord.Embed(title=str(timeout), description="2" + " punkt regulaminu", colour=discord.Colour.red())
                embed.add_field(name="Data otrzymania", value="<t:"+str(now.timestamp())[:-2]+":F>")
                embed.add_field(name="Data zakończenia", value="<t:"+str(enddate.timestamp())[:-2]+":F>")
                embed.add_field(name="Użytkownik", value=message.author.mention, inline=False)
                embed.add_field(name="Mod", value="<@"+str(self.user.id)+">")
                mes = await warn_channel.send(embed=embed)
                await self.pool.execute("INSERT INTO warn VALUES($1, $2, $3, $4, $5, $6, $7, $8);", message.author.id, 3, '2', now, enddate, mes.id, [self.user.id], True)
                await message.author.add_roles(timeout)
                await message.channel.send(message.author.mention + " otrzymał **" + str(timeout) + "** za 2 punkt regulaminu.")
            else:
                data = data[0]
                old_warn_role = discord.utils.get(message.guild.roles, name=config.warns[data['typ']])
                powod = data['powod'] + '/2'
                autorzy = data['autorzy'] + [self.user.id]
                embed = discord.Embed(title=str(timeout), description=powod + " punkt regulaminu", colour=discord.Colour.red())
                embed.add_field(name="Data otrzymania", value="<t:"+str(data['start'].timestamp())[:-2]+":F>")
                embed.add_field(name="Data zakończenia", value="<t:"+str(enddate.timestamp())[:-2]+":F>")
                embed.add_field(name="Użytkownik", value=message.author.mention, inline=False)
                for autor in autorzy:
                    embed.add_field(name="Mod", value="<@"+str(autor)+">")
                mes = warn_channel.get_partial_message(data['message_id'])
                await mes.edit(embed=embed)
                async with self.pool.acquire() as con:
                    await con.execute("DELETE FROM warn WHERE id=$1 AND active=$2;", message.author.id, False)
                    await con.execute('UPDATE warn SET active=$1 WHERE id=$2;', False, message.author.id)
                    await con.execute("INSERT INTO warn VALUES($1, $2, $3, $4, $5, $6, $7, $8);", message.author.id, 3, powod, data['start'], enddate, mes.id, autorzy, True)
                await message.author.add_roles(timeout)
                await message.author.remove_roles(old_warn_role)
                await message.channel.send(message.author.mention + " otrzymał **" + str(timeout) + "** za 2 punkt regulaminu.")

                
        if "buu" in message.content.lower():
            await message.channel.send("Waaa")
        if "jd" in message.content.lower():
            jd = discord.utils.get(message.guild.roles, name="JD")
            await message.author.add_roles(jd)
        if message.channel.id == config.szukanie_gry_channel_id and 'clash' in message.content.lower():
            await message.delete()
            try:
                await message.author.send("Na kanale #szukanie-gry obowiązuje zakaz szukania na clash. Przenieś się na kanał #clash. Próby ominięcia tego zakazu zakończą się dwoma warnami.")
            except discord.errors.Forbidden:
                pass

    async def on_member_join(self, member: discord.Member):
        if member.created_at + datetime.timedelta(hours=2) > datetime.datetime.now(datetime.timezone.utc):
            await member.ban(reason="Multikonto")
            channel = member.guild.get_channel(config.komendy_botowe_channel_id)
            await channel.send(f"Zbanowano {member.mention} za multikonto!")

"""ERROR HANDLER"""
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
file_handler = logging.handlers.RotatingFileHandler(filename='discord.log', encoding='utf-8', maxBytes=32 * 1024 * 1024, backupCount=3)
console_handler = logging.StreamHandler()
dt_fmt = '%Y-%m-%d %H:%M:%S'
formatter = logging.Formatter('[{asctime}] [{levelname:<8}] {name}: {message}', dt_fmt, style='{')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.addHandler(file_handler)
"""ERROR HANDLER"""

bot = Bot()

async def main():
    async with bot, asyncpg.create_pool(**config.POSTGRES_INFO) as pool:
        bot.pool = pool
        await bot.start(config.token)

asyncio.run(main())