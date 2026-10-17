from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_entries: dict[tuple[int, int], _Entry] = {}


@asynccontextmanager
async def member_role_lock(guild_id: int, user_id: int) -> AsyncIterator[None]:
    """Serialize local role writes, retaining entries only while in use or awaited."""
    key = (guild_id, user_id)
    entry = _entries.setdefault(key, _Entry())
    entry.users += 1
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        entry.users -= 1
        if not entry.users:
            del _entries[key]
