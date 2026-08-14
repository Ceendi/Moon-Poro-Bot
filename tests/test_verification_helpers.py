from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.cogs import verification
from moon_poro.cogs.verification import (
    VerificationStartView,
    _apply_verified_roles,
    _get_leagues,
    _refresh_next_verified,
    _remove_verified_roles,
)
from moon_poro.verification_sessions import CreatedVerificationSession


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


async def test_verification_start_creates_one_time_rso_link() -> None:
    member = Mock(spec=verification.discord.Member)
    member.id = 101
    member.roles = []
    guild = SimpleNamespace(id=123)
    response = SimpleNamespace(send_message=AsyncMock())
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verification_cooldown=30,
            verified_role_name="Verified",
            role_ids={},
            rso_session_ttl_seconds=600,
            rso_base_url="https://bot.example.com",
        ),
        verifications=SimpleNamespace(get_by_user=AsyncMock(return_value=None)),
        verification_sessions=SimpleNamespace(
            create=AsyncMock(
                return_value=CreatedVerificationSession(
                    token="a" * 43,
                    expires_at=verification.discord.utils.utcnow(),
                )
            )
        ),
    )
    view = VerificationStartView(bot)
    interaction = SimpleNamespace(
        user=member,
        guild=guild,
        response=response,
    )
    button = view.children[0]

    await button.callback(interaction)

    bot.verification_sessions.create.assert_awaited_once_with(
        guild_id=123,
        user_id=101,
        ttl_seconds=600,
    )
    response.send_message.assert_awaited_once()
    sent_view = response.send_message.await_args.kwargs["view"]
    assert sent_view.children[0].url == "https://bot.example.com/verify/start/" + "a" * 43
    assert response.send_message.await_args.kwargs["ephemeral"] is True


async def test_verification_start_is_rate_limited() -> None:
    member = Mock(spec=verification.discord.Member)
    member.id = 101
    member.roles = []
    response = SimpleNamespace(send_message=AsyncMock())
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verification_cooldown=30,
            verified_role_name="Verified",
            role_ids={},
            rso_session_ttl_seconds=600,
            rso_base_url="https://bot.example.com",
        ),
        verifications=SimpleNamespace(get_by_user=AsyncMock(return_value=None)),
        verification_sessions=SimpleNamespace(
            create=AsyncMock(
                return_value=CreatedVerificationSession(
                    token="a" * 43,
                    expires_at=verification.discord.utils.utcnow(),
                )
            )
        ),
    )
    view = VerificationStartView(bot)
    interaction = SimpleNamespace(
        user=member,
        guild=SimpleNamespace(id=123),
        response=response,
    )

    await view.children[0].callback(interaction)
    await view.children[0].callback(interaction)

    assert "Spróbuj ponownie" in response.send_message.await_args_list[1].args[0]


def _rank_refresh_settings() -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=123,
        rank_refresh_claim_timeout_seconds=300,
        rank_refresh_interval_hours=24,
        rank_refresh_retry_base_seconds=300,
    )


async def test_rank_refresh_worker_updates_one_due_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(discord_user_id=101, platform="EUN1", puuid="puuid")
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        complete_rank_refresh=AsyncMock(return_value=True),
        retry_rank_refresh=AsyncMock(),
        defer_rank_refresh=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]
    monkeypatch.setattr(verification, "_get_leagues", AsyncMock(return_value=leagues))

    await _refresh_next_verified(cog)

    verifications.claim_due_rank_refreshes.assert_awaited_once_with(
        123,
        limit=1,
        claim_timeout_seconds=300,
    )
    cog.apply_verified_roles.assert_awaited_once_with(member, "EUN1", leagues)
    verifications.complete_rank_refresh.assert_awaited_once_with(
        123,
        101,
        rank_tier="EMERALD",
        refresh_interval_hours=24,
    )
    verifications.retry_rank_refresh.assert_not_awaited()


async def test_rank_refresh_worker_retries_riot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(discord_user_id=101, platform="EUN1", puuid="puuid")
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        complete_rank_refresh=AsyncMock(),
        retry_rank_refresh=AsyncMock(return_value=300),
        defer_rank_refresh=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    monkeypatch.setattr(
        verification,
        "_get_leagues",
        AsyncMock(side_effect=verification.RiotAPIUnavailable),
    )

    await _refresh_next_verified(cog)

    verifications.retry_rank_refresh.assert_awaited_once_with(
        123,
        101,
        base_delay_seconds=300,
    )
    verifications.complete_rank_refresh.assert_not_awaited()


async def test_rank_refresh_worker_defers_member_outside_guild() -> None:
    link = SimpleNamespace(discord_user_id=101, platform="EUN1", puuid="puuid")
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=None))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        defer_rank_refresh=AsyncMock(return_value=True),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )

    await _refresh_next_verified(SimpleNamespace(bot=bot))

    verifications.defer_rank_refresh.assert_awaited_once_with(
        123,
        101,
        delay_seconds=86_400,
    )
