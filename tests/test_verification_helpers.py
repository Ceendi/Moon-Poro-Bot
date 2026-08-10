from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.cogs import verification
from moon_poro.cogs.verification import (
    ConfirmVerificationView,
    VerificationStartView,
    _apply_verified_roles,
    _get_leagues,
    _remove_verified_roles,
)


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


async def test_get_leagues_normalizes_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    riot_call = AsyncMock(return_value=None)
    monkeypatch.setattr(verification, "riot_api_call", riot_call)
    bot = SimpleNamespace(riot_client=SimpleNamespace())

    assert await _get_leagues(bot, "EUN1", "puuid") == []
    assert riot_call.await_args.kwargs["not_found"] == []


async def test_apply_verified_roles_replaces_managed_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_rank = FakeRole("Old")
    rank = FakeRole("Gold")
    server = FakeRole("EUNE")
    verified = FakeRole("Verified")
    member_role = FakeRole("Member")
    settings = SimpleNamespace(
        lol_ranks=["Old", "Gold"],
        lol_servers=["EUNE"],
        verified_role_name="Verified",
        member_role_name="Member",
    )
    bot = SimpleNamespace(settings=settings)
    member = SimpleNamespace(
        guild=object(),
        roles=[old_rank],
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    monkeypatch.setattr(verification, "get_discord_rank_role", lambda *_args: rank)
    monkeypatch.setattr(
        verification,
        "find_role",
        Mock(side_effect=[server, verified, member_role]),
    )
    monkeypatch.setattr(verification, "member_roles_named", lambda *_args: [old_rank])

    await _apply_verified_roles(
        bot,
        member,
        "EUN1",
        [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}],
    )

    member.remove_roles.assert_awaited_once_with(old_rank, reason="Synchronizacja weryfikacji Riot")
    added = set(member.add_roles.await_args.args)
    assert added == {rank, server, verified, member_role}


async def test_remove_verified_roles_removes_every_managed_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = [FakeRole("Gold"), FakeRole("EUNE"), FakeRole("Verified")]
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            lol_ranks=["Gold"],
            lol_servers=["EUNE"],
            verified_role_name="Verified",
        )
    )
    member = SimpleNamespace(remove_roles=AsyncMock())
    monkeypatch.setattr(verification, "member_roles_named", lambda *_args: managed)

    await _remove_verified_roles(bot, member, reason="test")

    member.remove_roles.assert_awaited_once_with(*managed, reason="test")


async def test_confirmation_timeout_disables_button() -> None:
    bot = SimpleNamespace(settings=SimpleNamespace(verification_timeout=60))
    view = ConfirmVerificationView(
        bot=bot,
        expected_icon_id=7,
        puuid="puuid",
        platform="EUN1",
    )
    view.message = SimpleNamespace(edit=AsyncMock())

    await view.on_timeout()

    assert all(item.disabled for item in view.children)
    view.message.edit.assert_awaited_once()


async def test_verification_start_opens_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verification_timeout=60,
            verification_cooldown=30,
            verified_role_name="Verified",
        )
    )
    view = VerificationStartView(bot)
    modal = object()
    monkeypatch.setattr(verification, "VerificationModal", Mock(return_value=modal))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101),
        response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
    )
    button = view.children[0]

    await button.callback(interaction)
    await button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once_with(modal)
    interaction.response.send_message.assert_awaited_once()
    assert "Spróbuj ponownie" in interaction.response.send_message.await_args.args[0]
