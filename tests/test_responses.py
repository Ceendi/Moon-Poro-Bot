from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from moon_poro import responses


def make_interaction(*, response_done: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        response=SimpleNamespace(
            is_done=Mock(return_value=response_done),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_safe_send_uses_initial_response_when_available() -> None:
    interaction = make_interaction()

    await responses.safe_send(interaction, "message", ephemeral=False)

    interaction.response.send_message.assert_awaited_once_with("message", ephemeral=False)
    interaction.followup.send.assert_not_awaited()


async def test_safe_send_uses_followup_after_response() -> None:
    interaction = make_interaction(response_done=True)

    await responses.safe_send(interaction, "message")

    interaction.followup.send.assert_awaited_once_with("message", ephemeral=True)
    interaction.response.send_message.assert_not_awaited()


async def test_safe_send_suppresses_discord_http_failure() -> None:
    interaction = make_interaction()
    http_response = Mock(status=500, reason="Server Error")
    interaction.response.send_message.side_effect = discord.HTTPException(http_response, "failed")

    await responses.safe_send(interaction, "message")

    interaction.response.send_message.assert_awaited_once()


async def test_handle_known_error_rejects_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_send = AsyncMock()
    monkeypatch.setattr(responses, "safe_send", safe_send)

    handled = await responses.handle_known_error(make_interaction(), app_commands.CheckFailure())

    assert handled
    safe_send.assert_awaited_once()
    assert "uprawnień" in safe_send.await_args.args[1]


async def test_handle_known_error_rounds_cooldown_up_to_one_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_send = AsyncMock()
    monkeypatch.setattr(responses, "safe_send", safe_send)
    error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 30), 0.1)
    interaction = make_interaction()

    handled = await responses.handle_known_error(interaction, error)

    assert handled
    safe_send.assert_awaited_once_with(
        interaction,
        "Spróbuj ponownie za 1 s.",
    )


async def test_handle_known_error_reports_missing_discord_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_send = AsyncMock()
    monkeypatch.setattr(responses, "safe_send", safe_send)
    http_response = Mock(status=403, reason="Forbidden")
    forbidden = discord.Forbidden(http_response, "missing permission")
    error = SimpleNamespace(original=forbidden)

    handled = await responses.handle_known_error(make_interaction(), error)

    assert handled
    safe_send.assert_awaited_once()
    assert "wymaganych uprawnień" in safe_send.await_args.args[1]


async def test_handle_known_error_leaves_unknown_errors_unhandled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_send = AsyncMock()
    monkeypatch.setattr(responses, "safe_send", safe_send)

    handled = await responses.handle_known_error(
        make_interaction(), app_commands.AppCommandError("unknown")
    )

    assert not handled
    safe_send.assert_not_awaited()
