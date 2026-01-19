import datetime
import logging
from typing import List

import discord

import config

logger = logging.getLogger('discord.helpers')


def has_role(member: discord.Member, role_name: str) -> bool:
    return any(role.name == role_name for role in member.roles)


def has_any_role(member: discord.Member, role_names: List[str]) -> bool:
    member_role_names = {role.name for role in member.roles}
    return bool(member_role_names.intersection(role_names))


async def update_mod_stats(pool, mod_id: int, stat_type: str = 'z'):
    async with pool.acquire() as con:
        mod_stat = await con.fetch('SELECT * FROM mod_stats WHERE id=$1;', mod_id)
        if not mod_stat:
            await con.execute("INSERT INTO mod_stats(id) VALUES($1);", mod_id)
        
        year = datetime.date.today().year % 100
        month = datetime.date.today().strftime('%m')
        column_name = f"{stat_type}y{year}_m{month}"
        
        await con.execute(f'''
            ALTER TABLE mod_stats ADD COLUMN IF NOT EXISTS zy{year}_m{month} SMALLINT DEFAULT 0;
            ALTER TABLE mod_stats ADD COLUMN IF NOT EXISTS wy{year}_m{month} SMALLINT DEFAULT 0;
        ''')
        await con.execute(f"UPDATE mod_stats SET {column_name}={column_name}+1 WHERE id=$1;", mod_id)


def build_warn_embed(
    title: str,
    powod: str,
    start: datetime.datetime,
    koniec: datetime.datetime,
    user_mention: str,
    autorzy: List[int],
    opis: str = None,
    colour: discord.Colour = discord.Colour.red()
) -> discord.Embed:
    embed = discord.Embed(title=title, description=f"{powod} punkt regulaminu", colour=colour)
    if opis:
        embed.add_field(name="Opis", value=opis, inline=False)
    embed.add_field(name="Data otrzymania", value=f"<t:{int(start.timestamp())}:F>")
    embed.add_field(name="Data zakończenia", value=f"<t:{int(koniec.timestamp())}:F>")
    embed.add_field(name="Użytkownik", value=user_mention, inline=False)
    for autor in autorzy:
        embed.add_field(name="Mod", value=f"<@{autor}>", inline=True)
    return embed


async def safe_api_call(coro, default=None):
    try:
        return await coro
    except Exception as e:
        if hasattr(e, 'status'):
            if e.status == 404:
                logger.debug(f"API 404: {e}")
            elif e.status == 429:
                logger.warning(f"API rate limited: {e}")
            else:
                logger.error(f"API error {e.status}: {e}")
        else:
            logger.error(f"API error: {e}")
        return default
