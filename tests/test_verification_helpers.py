from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.cogs import verification
from moon_poro.cogs.verification import (
    VerificationStartView,
    _apply_verified_roles,
    _get_leagues,
    _refresh_next_verified,
    _remove_verified_roles,
    _retry_next_rank_role_sync,
    _retry_next_rso_audit_cleanup,
    _retry_next_verified_marker_cleanup,
)
from moon_poro.rank_refresh import RankRefreshDecision
from moon_poro.riot import RiotAPINotFound, RiotAuthBreaker
from moon_poro.verification_sessions import CreatedVerificationSession


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


async def test_get_leagues_keeps_successful_empty_response_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    riot_call = AsyncMock(return_value=[])
    monkeypatch.setattr(verification, "riot_api_call", riot_call)
    bot = SimpleNamespace(riot_client=SimpleNamespace())

    assert await _get_leagues(bot, "EUN1", "puuid") == []
    assert "not_found" not in riot_call.await_args.kwargs


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
            guild_id=123,
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
            guild_id=123,
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
        guild_id=123,
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
            guild_id=123,
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
        guild_id=123,
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
        rank_refresh_policy="adaptive",
        rank_refresh_rollout_percent=100,
    )


async def test_rank_refresh_worker_updates_one_due_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        last_known_rank=None,
        rank_unranked_confirmations=0,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    checked_at = datetime(2026, 8, 15, tzinfo=UTC)
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        record_rank_snapshot=AsyncMock(return_value=checked_at),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        retry_rank_refresh=AsyncMock(),
        defer_rank_refresh=AsyncMock(),
        get_by_user=AsyncMock(return_value=link),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
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
    cog.apply_verified_roles.assert_awaited_once()
    assert cog.apply_verified_roles.await_args.args[2][0]["tier"] == "EMERALD"
    verifications.record_rank_snapshot.assert_awaited_once()
    verifications.acknowledge_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        expected_rank_last_checked_at=checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )
    verifications.retry_rank_refresh.assert_not_awaited()


async def test_rank_refresh_worker_retries_riot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        release_rank_refresh_claim=AsyncMock(),
        retry_rank_refresh=AsyncMock(return_value=300),
        defer_rank_refresh=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
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
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=link.created_at,
    )


async def test_rank_refresh_worker_treats_404_as_record_error_not_unranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="missing",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        retry_rank_refresh=AsyncMock(return_value=321),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    monkeypatch.setattr(
        verification,
        "_get_leagues",
        AsyncMock(side_effect=RiotAPINotFound),
    )

    await _refresh_next_verified(cog)

    verifications.retry_rank_refresh.assert_awaited_once_with(
        123,
        101,
        base_delay_seconds=300,
        expected_puuid="missing",
        expected_platform="EUN1",
        expected_created_at=link.created_at,
    )
    cog.apply_verified_roles.assert_not_awaited()


async def test_rank_refresh_worker_requires_two_empty_200_responses_for_unranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 8, 14, tzinfo=UTC)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=created_at,
        deletion_requested_at=None,
        last_known_rank="EMERALD",
        last_known_division="II",
        last_known_league_points=50,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_unranked_confirmations=0,
    )
    member = SimpleNamespace(id=101)
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    checked_times = iter(
        [
            datetime(2026, 8, 15, 1, tzinfo=UTC),
            datetime(2026, 8, 15, 2, tzinfo=UTC),
        ]
    )

    async def record_snapshot(*_args: object, **kwargs: object) -> datetime:
        decision = cast(RankRefreshDecision, kwargs["decision"])
        snapshot = decision.snapshot
        link.last_known_rank = snapshot.tier
        link.last_known_division = snapshot.division
        link.last_known_league_points = snapshot.league_points
        link.last_known_wins = snapshot.wins
        link.last_known_losses = snapshot.losses
        link.last_known_inactive = snapshot.inactive
        link.rank_unranked_confirmations = decision.unranked_confirmations
        link.rank_last_checked_at = next(checked_times)
        return link.rank_last_checked_at

    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        record_rank_snapshot=AsyncMock(side_effect=record_snapshot),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        retry_rank_refresh=AsyncMock(),
        defer_rank_refresh=AsyncMock(),
        get_by_user=AsyncMock(return_value=link),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    monkeypatch.setattr(verification, "_get_leagues", AsyncMock(return_value=[]))

    await _refresh_next_verified(cog)
    await _refresh_next_verified(cog)

    assert cog.apply_verified_roles.await_args_list[0].args[2][0]["tier"] == "EMERALD"
    assert cog.apply_verified_roles.await_args_list[1].args[2][0]["tier"] == "UNRANKED"
    assert link.rank_unranked_confirmations == 2


async def test_rank_refresh_worker_defers_member_outside_guild() -> None:
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=None))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        defer_rank_refresh=AsyncMock(return_value=True),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
    )

    await _refresh_next_verified(SimpleNamespace(bot=bot))

    verifications.defer_rank_refresh.assert_awaited_once_with(
        123,
        101,
        delay_seconds=7 * 86_400,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=link.created_at,
    )


async def test_discord_role_retry_uses_cached_snapshot_without_riot_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        last_known_rank="DIAMOND",
        last_known_division="IV",
        last_known_league_points=20,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=datetime(2026, 8, 15, tzinfo=UTC),
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    verifications = SimpleNamespace(
        claim_due_rank_role_syncs=AsyncMock(return_value=[link]),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        get_by_user=AsyncMock(return_value=link),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    riot_call = AsyncMock()
    monkeypatch.setattr(verification, "_get_leagues", riot_call)

    await _retry_next_rank_role_sync(cog)

    riot_call.assert_not_awaited()
    cog.apply_verified_roles.assert_awaited_once()
    assert cog.apply_verified_roles.await_args.args[2][0]["tier"] == "DIAMOND"
    verifications.acknowledge_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        expected_rank_last_checked_at=link.rank_last_checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )


async def test_orphan_rso_audit_http_failure_remains_in_durable_retry_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    delete = AsyncMock(side_effect=FakeHTTPException())
    channel = SimpleNamespace(get_partial_message=Mock(return_value=SimpleNamespace(delete=delete)))
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    cleanup = SimpleNamespace(id=7, guild_id=123, channel_id=654, message_id=987)
    sessions = SimpleNamespace(
        claim_audit_cleanups=AsyncMock(return_value=[cleanup]),
        retry_audit_cleanup=AsyncMock(return_value=300),
        acknowledge_audit_cleanup=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verification_sessions=sessions,
        get_guild=Mock(return_value=guild),
    )
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification.discord.abc, "Messageable", type(channel))

    await _retry_next_rso_audit_cleanup(SimpleNamespace(bot=bot))

    sessions.retry_audit_cleanup.assert_awaited_once_with(
        7,
        message_id=987,
        base_delay_seconds=300,
    )
    sessions.acknowledge_audit_cleanup.assert_not_awaited()


async def test_claimed_role_sync_compensates_when_delete_tombstone_wins_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="DIAMOND",
        last_known_division="IV",
        last_known_league_points=20,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    deleting = SimpleNamespace(**vars(link))
    deleting.deletion_requested_at = datetime(2026, 8, 15, 1, tzinfo=UTC)
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    verifications = SimpleNamespace(
        claim_due_rank_role_syncs=AsyncMock(return_value=[link]),
        get_by_user=AsyncMock(return_value=deleting),
        acknowledge_rank_role_sync=AsyncMock(),
        retry_rank_role_sync=AsyncMock(),
        enqueue_verified_marker_cleanup=AsyncMock(return_value=1),
        acknowledge_verified_marker_cleanup=AsyncMock(),
        retry_verified_marker_cleanup=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)

    await _retry_next_rank_role_sync(cog)

    cog.apply_verified_roles.assert_awaited_once()
    remove_marker.assert_awaited_once_with(
        bot,
        member,
        reason="Anulowanie nieaktualnej synchronizacji weryfikacji Riot",
    )
    verifications.acknowledge_rank_role_sync.assert_not_awaited()


async def test_failed_stale_marker_compensation_survives_worker_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    member = SimpleNamespace(id=101)
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    cleanup = SimpleNamespace(discord_user_id=101, generation=1)
    verifications = SimpleNamespace(
        get_by_user=AsyncMock(return_value=None),
        enqueue_verified_marker_cleanup=AsyncMock(return_value=1),
        retry_verified_marker_cleanup=AsyncMock(return_value=300),
        acknowledge_verified_marker_cleanup=AsyncMock(),
        claim_due_verified_marker_cleanups=AsyncMock(return_value=[cleanup]),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    failed_remove = AsyncMock(side_effect=FakeHTTPException())
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification, "_remove_verified_marker", failed_remove)

    assert not await verification._reconcile_applied_roles(cog, member, link)

    verifications.enqueue_verified_marker_cleanup.assert_awaited_once_with(123, 101)
    verifications.retry_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
        base_delay_seconds=300,
    )
    verifications.acknowledge_verified_marker_cleanup.assert_not_awaited()

    successful_remove = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", successful_remove)
    await _retry_next_verified_marker_cleanup(cog)

    successful_remove.assert_awaited_once_with(
        bot,
        member,
        reason="Dokończenie usuwania nieaktualnej roli weryfikacji Riot",
    )
    verifications.acknowledge_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
    )


async def test_marker_cleanup_restores_reverified_generation_that_wins_during_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    replacement = SimpleNamespace(
        platform="EUW1",
        puuid="replacement-puuid",
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="DIAMOND",
        last_known_division="IV",
        last_known_league_points=20,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=datetime(2026, 8, 15, 1, tzinfo=UTC),
    )
    cleanup = SimpleNamespace(discord_user_id=101, generation=1)
    verifications = SimpleNamespace(
        claim_due_verified_marker_cleanups=AsyncMock(return_value=[cleanup]),
        get_by_user=AsyncMock(side_effect=[None, replacement, replacement]),
        acknowledge_verified_marker_cleanup=AsyncMock(),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        retry_verified_marker_cleanup=AsyncMock(),
        schedule_rank_refresh_now=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)

    await _retry_next_verified_marker_cleanup(cog)

    remove_marker.assert_awaited_once()
    cog.apply_verified_roles.assert_awaited_once()
    assert cog.apply_verified_roles.await_args.args[1] == "EUW1"
    assert cog.apply_verified_roles.await_args.args[2][0]["tier"] == "DIAMOND"
    verifications.acknowledge_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        expected_rank_last_checked_at=replacement.rank_last_checked_at,
        expected_puuid="replacement-puuid",
        expected_platform="EUW1",
        expected_created_at=replacement.created_at,
    )
    verifications.acknowledge_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
    )


async def test_marker_cleanup_compensates_delete_during_replacement_role_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    replacement = SimpleNamespace(
        platform="EUW1",
        puuid="replacement-puuid",
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="DIAMOND",
        last_known_division="IV",
        last_known_league_points=20,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=datetime(2026, 8, 15, 1, tzinfo=UTC),
    )
    cleanup = SimpleNamespace(discord_user_id=101, generation=1)
    verifications = SimpleNamespace(
        claim_due_verified_marker_cleanups=AsyncMock(return_value=[cleanup]),
        get_by_user=AsyncMock(side_effect=[replacement, None, None, None]),
        acknowledge_verified_marker_cleanup=AsyncMock(),
        acknowledge_rank_role_sync=AsyncMock(),
        retry_rank_role_sync=AsyncMock(),
        retry_verified_marker_cleanup=AsyncMock(),
        schedule_rank_refresh_now=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)

    await _retry_next_verified_marker_cleanup(cog)

    cog.apply_verified_roles.assert_awaited_once()
    remove_marker.assert_awaited_once_with(
        bot,
        member,
        reason="Dokończenie usuwania nieaktualnej roli weryfikacji Riot",
    )
    verifications.acknowledge_rank_role_sync.assert_not_awaited()
    verifications.acknowledge_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
    )


async def test_reconcile_compensates_delete_during_replacement_role_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    guild = SimpleNamespace(id=123)
    member.guild = guild
    old_link = SimpleNamespace(
        platform="EUN1",
        puuid="old-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    replacement = SimpleNamespace(
        platform="EUW1",
        puuid="replacement-puuid",
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="DIAMOND",
        last_known_division="IV",
        last_known_league_points=20,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=datetime(2026, 8, 15, 1, tzinfo=UTC),
    )
    verifications = SimpleNamespace(
        get_by_user=AsyncMock(side_effect=[replacement, None, None, None]),
        enqueue_verified_marker_cleanup=AsyncMock(return_value=1),
        acknowledge_verified_marker_cleanup=AsyncMock(),
        acknowledge_rank_role_sync=AsyncMock(),
        retry_rank_role_sync=AsyncMock(),
        retry_verified_marker_cleanup=AsyncMock(),
        schedule_rank_refresh_now=AsyncMock(),
    )
    bot = SimpleNamespace(settings=_rank_refresh_settings(), verifications=verifications)
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)

    assert not await verification._reconcile_applied_roles(cog, member, old_link)

    cog.apply_verified_roles.assert_awaited_once()
    remove_marker.assert_awaited_once()
    verifications.acknowledge_rank_role_sync.assert_not_awaited()
    verifications.acknowledge_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
    )


async def test_rso_completion_uses_shared_snapshot_and_role_sync_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = datetime(2026, 8, 15, 10, tzinfo=UTC)
    checked_at = datetime(2026, 8, 15, 10, 1, tzinfo=UTC)
    member = SimpleNamespace(id=101, send=AsyncMock())
    guild = SimpleNamespace(
        id=123,
        name="Test",
        get_member=Mock(return_value=member),
        get_channel=Mock(return_value=None),
    )
    member.guild = guild
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        last_known_rank=None,
        rank_last_checked_at=None,
        rank_unranked_confirmations=0,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
    )
    verifications = SimpleNamespace(
        get_by_user=AsyncMock(return_value=link),
        record_rank_snapshot=AsyncMock(return_value=checked_at),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
    )
    sessions = SimpleNamespace(
        complete_discord=AsyncMock(return_value=True),
        retry_discord=AsyncMock(),
        fail_discord=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            rank_refresh_policy="adaptive",
            rank_refresh_rollout_percent=100,
            rank_refresh_interval_hours=24,
            rank_refresh_retry_base_seconds=300,
            rank_refresh_claim_timeout_seconds=300,
            zweryfikowani_channel_id=None,
        ),
        verifications=verifications,
        verification_sessions=sessions,
        get_guild=Mock(return_value=guild),
    )
    cog = object.__new__(verification.VerificationCog)
    cog.bot = bot
    cog.apply_verified_roles = AsyncMock()
    leagues = [
        {
            "queueType": "RANKED_SOLO_5x5",
            "tier": "EMERALD",
            "rank": "II",
            "leaguePoints": 50,
            "wins": 100,
            "losses": 90,
            "inactive": False,
        }
    ]
    monkeypatch.setattr(verification, "_get_leagues", AsyncMock(return_value=leagues))
    record = SimpleNamespace(
        id=1,
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        completion_attempts=1,
        created_at=created_at,
    )

    await cog._complete_rso_verification(record)

    verifications.record_rank_snapshot.assert_awaited_once()
    cog.apply_verified_roles.assert_awaited_once()
    verifications.acknowledge_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        expected_rank_last_checked_at=checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )
    sessions.complete_discord.assert_awaited_once_with(
        1,
        message_id=None,
        channel_id=None,
    )


async def test_rso_discord_retry_reuses_cached_snapshot_without_second_riot_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    created_at = datetime(2026, 8, 15, 10, tzinfo=UTC)
    checked_at = datetime(2026, 8, 15, 10, 1, tzinfo=UTC)
    member = SimpleNamespace(id=101, send=AsyncMock())
    guild = SimpleNamespace(
        id=123,
        name="Test",
        get_member=Mock(return_value=member),
        get_channel=Mock(return_value=None),
    )
    member.guild = guild
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="EMERALD",
        last_known_division="II",
        last_known_league_points=50,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=checked_at,
    )
    verifications = SimpleNamespace(
        get_by_user=AsyncMock(return_value=link),
        record_rank_snapshot=AsyncMock(),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(return_value=300),
    )
    sessions = SimpleNamespace(
        complete_discord=AsyncMock(return_value=True),
        retry_discord=AsyncMock(),
        fail_discord=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            rank_refresh_policy="adaptive",
            rank_refresh_rollout_percent=100,
            rank_refresh_interval_hours=24,
            rank_refresh_retry_base_seconds=300,
            rank_refresh_claim_timeout_seconds=300,
            zweryfikowani_channel_id=None,
        ),
        verifications=verifications,
        verification_sessions=sessions,
        get_guild=Mock(return_value=guild),
    )
    cog = object.__new__(verification.VerificationCog)
    cog.bot = bot
    cog.apply_verified_roles = AsyncMock(side_effect=[FakeHTTPException(), None])
    riot_call = AsyncMock()
    monkeypatch.setattr(verification, "_get_leagues", riot_call)
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    record = SimpleNamespace(
        id=1,
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        completion_attempts=1,
        created_at=created_at,
    )

    await cog._complete_rso_verification(record)
    await cog._complete_rso_verification(record)

    riot_call.assert_not_awaited()
    verifications.record_rank_snapshot.assert_not_awaited()
    verifications.retry_rank_role_sync.assert_awaited_once()
    sessions.retry_discord.assert_awaited_once()
    sessions.complete_discord.assert_awaited_once_with(
        1,
        message_id=None,
        channel_id=None,
    )


async def test_cancelled_rso_completion_uses_versioned_marker_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    checked_at = datetime(2026, 8, 15, 10, 1, tzinfo=UTC)
    member = SimpleNamespace(id=101, send=AsyncMock())
    guild = SimpleNamespace(
        id=123,
        name="Test",
        get_member=Mock(return_value=member),
        get_channel=Mock(return_value=None),
    )
    member.guild = guild
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="old-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="EMERALD",
        last_known_division="II",
        last_known_league_points=50,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=checked_at,
    )
    replacement = SimpleNamespace(**vars(link))
    replacement.puuid = "replacement-puuid"
    replacement.created_at = datetime(2026, 8, 15, 10, 2, tzinfo=UTC)
    verifications = SimpleNamespace(
        get_by_user=AsyncMock(side_effect=[link, link, replacement, None, None, None, None]),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        enqueue_verified_marker_cleanup=AsyncMock(return_value=1),
        acknowledge_verified_marker_cleanup=AsyncMock(),
        retry_verified_marker_cleanup=AsyncMock(),
        claim_due_verified_marker_cleanups=AsyncMock(
            return_value=[SimpleNamespace(discord_user_id=101, generation=1)]
        ),
        schedule_rank_refresh_now=AsyncMock(),
    )
    sessions = SimpleNamespace(
        complete_discord=AsyncMock(return_value=False),
        retry_discord=AsyncMock(),
        fail_discord=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            rank_refresh_policy="adaptive",
            rank_refresh_rollout_percent=100,
            rank_refresh_interval_hours=24,
            rank_refresh_retry_base_seconds=300,
            rank_refresh_claim_timeout_seconds=300,
            zweryfikowani_channel_id=None,
        ),
        verifications=verifications,
        verification_sessions=sessions,
        get_guild=Mock(return_value=guild),
    )
    cog = object.__new__(verification.VerificationCog)
    cog.bot = bot
    cog.apply_verified_roles = AsyncMock()
    remove_marker = AsyncMock(side_effect=[FakeHTTPException(), None])
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    record = SimpleNamespace(
        id=1,
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="old-puuid",
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        completion_attempts=1,
        created_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )

    await cog._complete_rso_verification(record)

    assert cog.apply_verified_roles.await_count == 2
    verifications.enqueue_verified_marker_cleanup.assert_awaited_once_with(123, 101)
    verifications.retry_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
        base_delay_seconds=300,
    )
    verifications.acknowledge_verified_marker_cleanup.assert_not_awaited()

    await _retry_next_verified_marker_cleanup(cog)

    assert remove_marker.await_count == 2
    verifications.acknowledge_verified_marker_cleanup.assert_awaited_once_with(
        123,
        101,
        expected_generation=1,
    )
