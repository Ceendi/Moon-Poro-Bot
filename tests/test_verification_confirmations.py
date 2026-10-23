from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from moon_poro.account_profile import build_verification_confirmation
from moon_poro.cogs import verification
from moon_poro.cogs.verification import VerificationCog
from moon_poro.verification_confirmations import ConfirmationResponses


def _interaction(*, expires_in: timedelta = timedelta(minutes=10)) -> SimpleNamespace:
    return SimpleNamespace(
        expires_at=datetime.now(UTC) + expires_in,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _link(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "platform": "EUN1",
        "last_known_rank": "DIAMOND",
        "last_known_division": "II",
        "last_known_league_points": 40,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _record(*, completed_ago: timedelta = timedelta(seconds=5)) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        guild_id=123,
        discord_user_id=101,
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        discord_guild_name="Moon Poro",
        completed_at=datetime.now(UTC) - completed_ago,
    )


def _cog(user: object | None = None) -> VerificationCog:
    cog = object.__new__(VerificationCog)
    cog.bot = SimpleNamespace(
        verification_confirmations=ConfirmationResponses(),
        verification_sessions=SimpleNamespace(finish_confirmation=AsyncMock()),
        rank_emojis={},
        get_guild=Mock(return_value=SimpleNamespace(name="Moon Poro")),
        get_user=Mock(return_value=user),
        fetch_user=AsyncMock(return_value=user),
    )
    return cog


def _http_error(cls: type[discord.HTTPException], status: int, code: int) -> Exception:
    return cls(SimpleNamespace(status=status, reason="error"), {"code": code, "message": "error"})


def test_registry_ignores_an_older_interaction_and_prunes_expired_tokens() -> None:
    responses = ConfirmationResponses()
    first = _interaction()
    newer = _interaction()
    responses.register(1, first)
    responses.register(1, newer)

    responses.mark_ready(1, first)
    responses.discard(1, first)
    pending = responses.get(1)
    assert pending is not None and pending.interaction is newer
    assert not pending.ready.is_set()
    responses.mark_ready(1, newer)
    assert pending.ready.is_set()

    responses.register(2, _interaction(expires_in=timedelta(seconds=30)))
    responses.prune()
    assert responses.get(2) is None
    assert responses.get(1) is pending


def test_confirmation_shows_riot_id_region_and_rank() -> None:
    embed = build_verification_confirmation(
        _link(), riot_id="Moon_Poro#EUNE", rank_emojis={"DIAMOND": "<:rank_diamond:1>"}
    )

    assert embed.title == "Weryfikacja udana"
    assert embed.description == "Role regionu i rangi masz już na serwerze."
    assert [(field.name, field.value) for field in embed.fields] == [
        ("Riot ID", "Moon\\_Poro#EUNE"),
        ("Region", "EUNE"),
        ("Solo/Duo", "<:rank_diamond:1> Diamond II"),
    ]


def test_confirmation_never_guesses_a_rank_riot_did_not_return() -> None:
    embed = build_verification_confirmation(
        _link(last_known_rank=None, last_known_division=None),
        riot_id="Moon#EUNE",
        guild_name="Moon *Poro*",
    )

    assert embed.fields[2].value == "Czekamy na Riot"
    assert embed.description == (
        "Rolę regionu masz już na serwerze **Moon \\*Poro\\***. "
        "Rolę rangi bot doda, gdy Riot potwierdzi rangę."
    )
    text = f"{embed.title}{embed.description}{[field.value for field in embed.fields]}"
    # Em and en dashes are banned in user-facing Polish texts.
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_confirmation_shows_a_confirmed_unranked_account() -> None:
    embed = build_verification_confirmation(
        _link(last_known_rank="UNRANKED", last_known_division=None), riot_id="Moon#EUNE"
    )

    assert embed.fields[2].value == "Brak rangi"
    assert embed.description == "Role regionu i rangi masz już na serwerze."


async def test_confirmation_goes_as_private_followup_to_the_link_message() -> None:
    cog = _cog()
    interaction = _interaction()
    cog.bot.verification_confirmations.register(7, interaction)
    cog.bot.verification_confirmations.mark_ready(7, interaction)

    await cog._deliver_confirmation(_record(), _link())

    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Weryfikacja udana"
    assert "serwerze **" not in kwargs["embed"].description
    cog.bot.verification_sessions.finish_confirmation.assert_awaited_once_with(7)
    assert cog.bot.verification_confirmations.get(7) is None
    cog.bot.get_user.assert_not_called()


async def test_followup_waits_until_the_handler_sent_its_own_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "CONFIRMATION_READY_TIMEOUT_SECONDS", 0.01)
    user = SimpleNamespace(send=AsyncMock())
    cog = _cog(user)
    interaction = _interaction()
    cog.bot.verification_confirmations.register(7, interaction)

    await cog._deliver_confirmation(_record(), _link())

    interaction.followup.send.assert_not_awaited()
    user.send.assert_not_awaited()
    cog.bot.verification_sessions.finish_confirmation.assert_not_awaited()
    assert cog.bot.verification_confirmations.get(7) is not None

    cog.bot.verification_confirmations.mark_ready(7, interaction)
    await cog._deliver_confirmation(_record(), _link())
    interaction.followup.send.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [
        _http_error(discord.HTTPException, 401, 50027),
        _http_error(discord.NotFound, 404, 10015),
    ],
)
async def test_expired_interaction_token_falls_back_to_dm(error: Exception) -> None:
    user = SimpleNamespace(send=AsyncMock())
    cog = _cog(user)
    interaction = _interaction()
    interaction.followup.send.side_effect = error
    cog.bot.verification_confirmations.register(7, interaction)
    cog.bot.verification_confirmations.mark_ready(7, interaction)

    await cog._deliver_confirmation(_record(), _link())

    kwargs = user.send.await_args.kwargs
    assert kwargs["nonce"] == "verified:7"
    assert "na serwerze **Moon Poro**" in kwargs["embed"].description
    cog.bot.verification_sessions.finish_confirmation.assert_awaited_once_with(7)
    assert cog.bot.verification_confirmations.get(7) is None


async def test_token_close_to_expiry_skips_the_followup() -> None:
    user = SimpleNamespace(send=AsyncMock())
    cog = _cog(user)
    interaction = _interaction(expires_in=timedelta(seconds=30))
    cog.bot.verification_confirmations.register(7, interaction)
    cog.bot.verification_confirmations.mark_ready(7, interaction)

    await cog._deliver_confirmation(_record(), _link())

    interaction.followup.send.assert_not_awaited()
    user.send.assert_awaited_once()


async def test_unclear_followup_failure_retries_once_then_uses_dm() -> None:
    user = SimpleNamespace(send=AsyncMock())
    cog = _cog(user)
    interaction = _interaction()
    interaction.followup.send.side_effect = TimeoutError
    cog.bot.verification_confirmations.register(7, interaction)
    cog.bot.verification_confirmations.mark_ready(7, interaction)

    await cog._deliver_confirmation(_record(), _link())
    user.send.assert_not_awaited()
    cog.bot.verification_sessions.finish_confirmation.assert_not_awaited()

    await cog._deliver_confirmation(_record(), _link())
    assert interaction.followup.send.await_count == 2
    user.send.assert_awaited_once()
    cog.bot.verification_sessions.finish_confirmation.assert_awaited_once_with(7)


async def test_after_restart_a_recent_confirmation_goes_by_dm() -> None:
    user = SimpleNamespace(send=AsyncMock())
    cog = _cog(user)

    await cog._deliver_confirmation(_record(), _link())

    user.send.assert_awaited_once()
    cog.bot.verification_sessions.finish_confirmation.assert_awaited_once_with(7)


async def test_old_confirmation_is_dropped_without_a_late_dm() -> None:
    user = SimpleNamespace(send=AsyncMock())
    cog = _cog(user)

    await cog._deliver_confirmation(_record(completed_ago=timedelta(hours=2)), _link())

    user.send.assert_not_awaited()
    cog.bot.verification_sessions.finish_confirmation.assert_awaited_once_with(7)


async def test_closed_dms_end_the_confirmation() -> None:
    user = SimpleNamespace(send=AsyncMock(side_effect=_http_error(discord.Forbidden, 403, 50007)))
    cog = _cog(user)

    await cog._deliver_confirmation(_record(), _link())

    cog.bot.verification_sessions.finish_confirmation.assert_awaited_once_with(7)


async def test_discord_outage_keeps_the_dm_for_a_retry() -> None:
    user = SimpleNamespace(
        send=AsyncMock(side_effect=_http_error(discord.DiscordServerError, 503, 0))
    )
    cog = _cog(user)

    await cog._deliver_confirmation(_record(), _link())

    cog.bot.verification_sessions.finish_confirmation.assert_not_awaited()


async def test_worker_delivers_each_claimed_confirmation() -> None:
    cog = _cog(SimpleNamespace(send=AsyncMock()))
    first, second = _record(), _record()
    second.id = 8
    cog.bot.verification_sessions.claim_confirmations = AsyncMock(
        return_value=[(first, _link()), (second, _link())]
    )
    cog._deliver_confirmation = AsyncMock(side_effect=[RuntimeError("boom"), None])

    await cog.deliver_verification_confirmations.coro(cog)

    cog.bot.verification_sessions.claim_confirmations.assert_awaited_once_with(
        limit=5, lease_seconds=verification.CONFIRMATION_LEASE_SECONDS
    )
    assert cog._deliver_confirmation.await_count == 2
    await asyncio.sleep(0)
