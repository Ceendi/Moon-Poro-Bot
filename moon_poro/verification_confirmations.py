"""In-memory interactions that receive the verification confirmation.

An interaction token lets the bot post a private followup for 15 minutes. It
is a secret, so it never leaves the bot process: after a restart the
confirmation goes by DM instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import discord

# Leave room for the request itself before Discord rejects the token.
TOKEN_SAFETY_MARGIN: Final = timedelta(seconds=60)


@dataclass(slots=True)
class PendingResponse:
    interaction: discord.Interaction
    # Set once the handler sent its own reply, so the followup lands below it.
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    attempts: int = 0

    def usable(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.interaction.expires_at - TOKEN_SAFETY_MARGIN > now


class ConfirmationResponses:
    """Map verification session IDs to the interaction that should get the result."""

    def __init__(self) -> None:
        self._pending: dict[int, PendingResponse] = {}

    def register(self, session_id: int, interaction: discord.Interaction) -> None:
        # A newer click (resume) replaces the older, soon expiring interaction.
        self._pending[session_id] = PendingResponse(interaction)

    def mark_ready(self, session_id: int, interaction: discord.Interaction) -> None:
        pending = self._pending.get(session_id)
        if pending is not None and pending.interaction is interaction:
            pending.ready.set()

    def discard(self, session_id: int, interaction: discord.Interaction | None = None) -> None:
        pending = self._pending.get(session_id)
        if pending is not None and (interaction is None or pending.interaction is interaction):
            del self._pending[session_id]

    def get(self, session_id: int) -> PendingResponse | None:
        return self._pending.get(session_id)

    def prune(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for session_id in [key for key, value in self._pending.items() if not value.usable(now)]:
            del self._pending[session_id]
