from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro import member_role_lock as member_role_lock_module
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
from moon_poro.repositories import VerificationLinkIdentity
from moon_poro.riot import RiotAPINotFound, RiotAuthBreaker
from moon_poro.verification_sessions import CreatedVerificationSession


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


def _guarded_verifications(
    *links: SimpleNamespace,
    guard_results: list[bool] | None = None,
    **attributes: object,
) -> SimpleNamespace:
    expected = {
        (
            link.guild_id,
            link.discord_user_id,
            link.puuid,
            link.platform,
            link.created_at,
        )
        for link in links
    }
    results = iter(guard_results or [])

    async def run_guarded_role_update(
        guild_id: int,
        user_id: int,
        *,
        identity: VerificationLinkIdentity,
        operation: Callable[[], Awaitable[None]],
        expected_rank_last_checked_at: datetime | None,
    ) -> bool:
        assert isinstance(identity, VerificationLinkIdentity)
        assert (
            guild_id,
            user_id,
            identity.puuid,
            identity.platform,
            identity.created_at,
        ) in expected
        result = next(results, True)
        if result:
            await operation()
        return result

    return SimpleNamespace(
        **attributes,
        run_verification_role_update_with_identity=AsyncMock(side_effect=run_guarded_role_update),
    )


async def test_get_leagues_keeps_successful_empty_response_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    riot_call = AsyncMock(return_value=[])
    monkeypatch.setattr(verification, "riot_api_call", riot_call)
    bot = SimpleNamespace(riot_client=SimpleNamespace())

    assert await _get_leagues(bot, "EUN1", "puuid") == []
    assert "not_found" not in riot_call.await_args.kwargs


@pytest.mark.parametrize(
    ("platform", "expected_region"),
    [("EUN1", "europe"), ("NA1", "americas")],
)
async def test_riot_id_is_synced_through_the_claim_fenced_repository(
    platform: str,
    expected_region: str,
) -> None:
    claimed_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform=platform,
        puuid="player-puuid",
        riot_game_name=None,
        riot_tag_line=None,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    account_lookup = AsyncMock(return_value={"gameName": " Moon ", "tagLine": " EUNE "})
    sync = AsyncMock(return_value=True)
    bot = SimpleNamespace(
        riot_client=SimpleNamespace(get_account_v1_by_puuid=account_lookup),
        verifications=SimpleNamespace(sync_riot_id_if_current=sync),
    )

    assert await verification._sync_riot_id(
        bot,
        link,
        expected_claimed_at=claimed_at,
    )

    account_lookup.assert_awaited_once_with(puuid="player-puuid", region=expected_region)
    sync.assert_awaited_once_with(
        123,
        101,
        game_name=" Moon ",
        tag_line=" EUNE ",
        expected_puuid="player-puuid",
        expected_platform=platform,
        expected_created_at=link.created_at,
        expected_claimed_at=claimed_at,
    )
    assert (link.riot_game_name, link.riot_tag_line) == ("Moon", "EUNE")


async def test_riot_id_sync_failure_does_not_raise_or_overwrite_last_known_id() -> None:
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="player-puuid",
        riot_game_name="Previous name",
        riot_tag_line="OLD",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    sync = AsyncMock()
    bot = SimpleNamespace(
        riot_client=SimpleNamespace(
            get_account_v1_by_puuid=AsyncMock(
                side_effect=verification.RiotAPIUnavailable(status=503)
            )
        ),
        verifications=SimpleNamespace(sync_riot_id_if_current=sync),
    )

    assert not await verification._sync_riot_id(
        bot,
        link,
        expected_claimed_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    sync.assert_not_awaited()
    assert (link.riot_game_name, link.riot_tag_line) == ("Previous name", "OLD")


@pytest.mark.parametrize(
    "account",
    [None, {}, {"gameName": "", "tagLine": "EUNE"}, {"gameName": "Moon"}],
)
async def test_malformed_riot_id_response_does_not_overwrite_last_known_id(
    account: object,
) -> None:
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="player-puuid",
        riot_game_name="Previous name",
        riot_tag_line="OLD",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    sync = AsyncMock()
    bot = SimpleNamespace(
        riot_client=SimpleNamespace(get_account_v1_by_puuid=AsyncMock(return_value=account)),
        verifications=SimpleNamespace(sync_riot_id_if_current=sync),
    )

    assert not await verification._sync_riot_id(
        bot,
        link,
        expected_claimed_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    sync.assert_not_awaited()
    assert (link.riot_game_name, link.riot_tag_line) == ("Previous name", "OLD")


async def test_missing_riot_id_response_does_not_overwrite_last_known_id() -> None:
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="player-puuid",
        riot_game_name="Previous name",
        riot_tag_line="OLD",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    sync = AsyncMock()
    bot = SimpleNamespace(
        riot_client=SimpleNamespace(
            get_account_v1_by_puuid=AsyncMock(side_effect=RiotAPINotFound(status=404))
        ),
        verifications=SimpleNamespace(sync_riot_id_if_current=sync),
    )

    assert not await verification._sync_riot_id(
        bot,
        link,
        expected_claimed_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    sync.assert_not_awaited()
    assert (link.riot_game_name, link.riot_tag_line) == ("Previous name", "OLD")


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
        id=101,
        guild=SimpleNamespace(fetch_member=AsyncMock()),
        roles=[old_rank],
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    member.guild.fetch_member.return_value = member
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


async def test_apply_verified_roles_treats_a_corrupt_cached_tier_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeRole("EUNE")
    verified = FakeRole("Verified")
    member_role = FakeRole("Member")
    settings = SimpleNamespace(
        lol_ranks=["Gold"],
        lol_servers=["EUNE"],
        verified_role_name="Verified",
        member_role_name="Member",
    )
    member = SimpleNamespace(
        id=101,
        guild=SimpleNamespace(fetch_member=AsyncMock()),
        roles=[],
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    member.guild.fetch_member.return_value = member
    monkeypatch.setattr(
        verification,
        "find_role",
        Mock(side_effect=[server, verified, member_role]),
    )
    monkeypatch.setattr(verification, "member_roles_named", lambda *_args: [])

    await _apply_verified_roles(
        SimpleNamespace(settings=settings),
        member,
        "EUN1",
        [{"queueType": "RANKED_SOLO_5x5", "tier": "WOOD"}],
    )

    assert set(member.add_roles.await_args.args) == {server, verified, member_role}


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
    guild = SimpleNamespace(id=123, name="Moon Poro")
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
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
        edit_original_response=AsyncMock(),
    )
    button = view.children[0]

    await button.callback(interaction)

    bot.verification_sessions.create.assert_awaited_once_with(
        guild_id=123,
        user_id=101,
        ttl_seconds=600,
        discord_username=str(member),
        discord_guild_name="Moon Poro",
    )
    response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    response.send_message.assert_not_awaited()
    sent_view = interaction.edit_original_response.await_args.kwargs["view"]
    assert sent_view.children[0].url == "https://bot.example.com/verify/start/" + "a" * 43
    assert sent_view.children[0].label == "Zaloguj się przez Riot"
    sent_embed = interaction.edit_original_response.await_args.kwargs["embed"]
    assert sent_embed.title == "Połącz konto Riot"
    assert sent_embed.description == (
        "Kliknij przycisk poniżej i zaloguj się na stronie Riot.\n"
        "Moon Poro nie zobaczy Twojego hasła. Link działa tylko dla Ciebie i wygaśnie za 10 min. Nie przekazuj go nikomu."
    )
    assert "token" not in sent_embed.description.lower()
    assert interaction.edit_original_response.await_args.kwargs["content"] is None


async def test_verification_start_is_rate_limited() -> None:
    member = Mock(spec=verification.discord.Member)
    member.id = 101
    member.roles = []
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
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
        guild=SimpleNamespace(id=123, name="Moon Poro"),
        guild_id=123,
        response=response,
        edit_original_response=AsyncMock(),
    )

    await view.children[0].callback(interaction)
    await view.children[0].callback(interaction)

    assert "Spróbuj ponownie" in response.send_message.await_args.args[0]


def _rank_refresh_settings() -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=123,
        rank_refresh_claim_timeout_seconds=300,
        rank_refresh_interval_hours=24,
        rank_refresh_retry_base_seconds=300,
        rank_refresh_policy="adaptive",
        rank_refresh_rollout_percent=100,
    )


async def test_completion_retry_logs_only_the_fresh_pause(caplog: pytest.LogCaptureFixture) -> None:
    sessions = SimpleNamespace(retry_discord=AsyncMock(side_effect=[True, False]))
    cog = object.__new__(verification.VerificationCog)
    cog.bot = SimpleNamespace(verification_sessions=sessions)
    record = SimpleNamespace(id=99, completion_attempts=8)
    await cog._retry_rso_completion(record, "DISCORD_AUDIT_UNAVAILABLE")
    await cog._retry_rso_completion(record, "DISCORD_AUDIT_UNAVAILABLE")
    assert sessions.retry_discord.await_args.kwargs["max_attempts"] == 8
    alerts = [item for item in caplog.records if item.levelname == "ERROR"]
    assert len(alerts) == 1
    assert "paused" in alerts[0].message


_RANK_REFRESH_CLAIMED_AT = datetime(2026, 8, 14, 12, tzinfo=UTC)


async def test_rank_refresh_worker_updates_one_due_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        last_known_rank=None,
        rank_last_checked_at=None,
        rank_unranked_confirmations=0,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    checked_at = datetime(2026, 8, 15, tzinfo=UTC)
    account_lookup = AsyncMock(return_value={"gameName": "Renamed Moon", "tagLine": "EUW"})
    sync_riot_id = AsyncMock(return_value=True)
    verifications = _guarded_verifications(
        link,
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        sync_riot_id_if_current=sync_riot_id,
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
        riot_client=SimpleNamespace(get_account_v1_by_puuid=account_lookup),
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
    assert (
        verifications.record_rank_snapshot.await_args.kwargs["expected_claimed_at"]
        == _RANK_REFRESH_CLAIMED_AT
    )
    assert (
        verifications.record_rank_snapshot.await_args.kwargs["expected_rank_last_checked_at"]
        is None
    )
    account_lookup.assert_awaited_once_with(puuid="puuid", region="europe")
    sync_riot_id.assert_awaited_once_with(
        123,
        101,
        game_name="Renamed Moon",
        tag_line="EUW",
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=link.created_at,
        expected_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    assert (link.riot_game_name, link.riot_tag_line) == ("Renamed Moon", "EUW")
    verifications.acknowledge_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        expected_rank_last_checked_at=checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )
    verifications.retry_rank_refresh.assert_not_awaited()


async def test_rank_refresh_worker_records_and_syncs_roles_when_riot_id_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        riot_game_name="Previous name",
        riot_tag_line="OLD",
        last_known_rank=None,
        rank_last_checked_at=None,
        rank_unranked_confirmations=0,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    checked_at = datetime(2026, 8, 15, tzinfo=UTC)
    sync_riot_id = AsyncMock()
    verifications = _guarded_verifications(
        link,
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        sync_riot_id_if_current=sync_riot_id,
        record_rank_snapshot=AsyncMock(return_value=checked_at),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        retry_rank_refresh=AsyncMock(),
        defer_rank_refresh=AsyncMock(),
        get_by_user=AsyncMock(return_value=link),
    )
    account_lookup = AsyncMock(side_effect=verification.RiotAPIUnavailable(status=503))
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        riot_client=SimpleNamespace(get_account_v1_by_puuid=account_lookup),
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())
    monkeypatch.setattr(
        verification,
        "_get_leagues",
        AsyncMock(return_value=[{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]),
    )

    await _refresh_next_verified(cog)

    account_lookup.assert_awaited_once_with(puuid="puuid", region="europe")
    sync_riot_id.assert_not_awaited()
    assert (link.riot_game_name, link.riot_tag_line) == ("Previous name", "OLD")
    verifications.record_rank_snapshot.assert_awaited_once()
    cog.apply_verified_roles.assert_awaited_once()


async def test_rank_refresh_worker_does_not_apply_roles_after_losing_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        last_known_rank=None,
        rank_last_checked_at=None,
        rank_unranked_confirmations=0,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        record_rank_snapshot=AsyncMock(return_value=None),
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
        AsyncMock(return_value=[{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]),
    )

    await _refresh_next_verified(cog)

    assert (
        verifications.record_rank_snapshot.await_args.kwargs["expected_claimed_at"]
        == _RANK_REFRESH_CLAIMED_AT
    )
    assert (
        verifications.record_rank_snapshot.await_args.kwargs["expected_rank_last_checked_at"]
        is None
    )
    cog.apply_verified_roles.assert_not_awaited()


async def test_rank_refresh_worker_retries_riot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
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
        expected_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )


async def test_rank_refresh_worker_releases_only_its_claim_after_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        release_rank_refresh_claim=AsyncMock(return_value=True),
        retry_rank_refresh=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    monkeypatch.setattr(
        verification,
        "_get_leagues",
        AsyncMock(side_effect=verification.RiotAPIUnavailable(status=401)),
    )

    await _refresh_next_verified(SimpleNamespace(bot=bot))

    verifications.release_rank_refresh_claim.assert_awaited_once_with(
        123,
        101,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=link.created_at,
        expected_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    verifications.retry_rank_refresh.assert_not_awaited()


async def test_rank_refresh_worker_backs_off_after_a_single_forbidden_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    verifications = SimpleNamespace(
        claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        release_rank_refresh_claim=AsyncMock(return_value=True),
        retry_rank_refresh=AsyncMock(return_value=300),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    monkeypatch.setattr(
        verification,
        "_get_leagues",
        AsyncMock(side_effect=verification.RiotAPIUnavailable(status=403)),
    )

    await _refresh_next_verified(SimpleNamespace(bot=bot))

    verifications.release_rank_refresh_claim.assert_not_awaited()
    verifications.retry_rank_refresh.assert_awaited_once()


async def test_rank_refresh_worker_rejects_a_claim_without_ownership_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        rank_refresh_claimed_at=None,
    )
    riot_call = AsyncMock()
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=SimpleNamespace(
            claim_due_rank_refreshes=AsyncMock(return_value=[link]),
        ),
        get_guild=Mock(return_value=SimpleNamespace(id=123)),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    monkeypatch.setattr(verification, "_get_leagues", riot_call)

    await _refresh_next_verified(SimpleNamespace(bot=bot))

    riot_call.assert_not_awaited()


async def test_rank_refresh_worker_treats_404_as_record_error_not_unranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        discord_user_id=101,
        platform="EUN1",
        puuid="missing",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
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
        expected_claimed_at=_RANK_REFRESH_CLAIMED_AT,
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
        riot_game_name="Moon",
        riot_tag_line="EUNE",
        created_at=created_at,
        deletion_requested_at=None,
        last_known_rank="EMERALD",
        last_known_division="II",
        last_known_league_points=50,
        last_known_wins=100,
        last_known_losses=90,
        last_known_inactive=False,
        rank_last_checked_at=None,
        rank_unranked_confirmations=0,
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
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

    verifications = _guarded_verifications(
        link,
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
        rank_refresh_claimed_at=_RANK_REFRESH_CLAIMED_AT,
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
        expected_claimed_at=_RANK_REFRESH_CLAIMED_AT,
    )


async def test_discord_role_retry_uses_cached_snapshot_without_riot_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    link = SimpleNamespace(
        guild_id=123,
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
    verifications = _guarded_verifications(
        link,
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
        guild_id=123,
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
    guild.fetch_member = AsyncMock(return_value=member)
    member.guild = guild
    verifications = _guarded_verifications(
        link,
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
    guild.fetch_member = AsyncMock(return_value=member)
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
    guild.fetch_member = AsyncMock(return_value=member)
    member.guild = guild
    replacement = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
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
    verifications = _guarded_verifications(
        replacement,
        claim_due_verified_marker_cleanups=AsyncMock(return_value=[cleanup]),
        get_by_user=AsyncMock(side_effect=[None, None, replacement, replacement]),
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
    guild.fetch_member = AsyncMock(return_value=member)
    member.guild = guild
    replacement = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
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
    verifications = _guarded_verifications(
        replacement,
        claim_due_verified_marker_cleanups=AsyncMock(return_value=[cleanup]),
        get_by_user=AsyncMock(side_effect=[replacement, None, None, None, None]),
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
    guild.fetch_member = AsyncMock(return_value=member)
    member.guild = guild
    old_link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="old-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    replacement = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
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
    verifications = _guarded_verifications(
        replacement,
        get_by_user=AsyncMock(side_effect=[replacement, None, None, None, None]),
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
    verifications = _guarded_verifications(
        link,
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
    cog._publish_rso_audit = AsyncMock(return_value=True)
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
    assert verifications.record_rank_snapshot.await_args.kwargs["expected_claimed_at"] is None
    assert (
        verifications.record_rank_snapshot.await_args.kwargs["expected_rank_last_checked_at"]
        is None
    )
    cog.apply_verified_roles.assert_awaited_once()
    verifications.acknowledge_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        expected_rank_last_checked_at=checked_at,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )
    cog._publish_rso_audit.assert_awaited_once_with(record)
    member.send.assert_awaited_once_with(
        "Twoje konto Riot jest połączone. Na serwerze **Test** masz już role regionu i rangi."
    )


async def test_rso_completion_retries_if_rank_worker_owns_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
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
        record_rank_snapshot=AsyncMock(return_value=None),
    )
    sessions = SimpleNamespace(
        retry_discord=AsyncMock(),
        fail_discord=AsyncMock(),
        complete_discord=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            rank_refresh_policy="adaptive",
            rank_refresh_rollout_percent=100,
            rank_refresh_interval_hours=24,
        ),
        verifications=verifications,
        verification_sessions=sessions,
        get_guild=Mock(return_value=guild),
    )
    cog = object.__new__(verification.VerificationCog)
    cog.bot = bot
    cog.apply_verified_roles = AsyncMock()
    monkeypatch.setattr(
        verification,
        "_get_leagues",
        AsyncMock(return_value=[{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]),
    )
    record = SimpleNamespace(
        id=2,
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        completion_attempts=1,
        created_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )

    await cog._complete_rso_verification(record)

    assert verifications.record_rank_snapshot.await_args.kwargs["expected_claimed_at"] is None
    assert (
        verifications.record_rank_snapshot.await_args.kwargs["expected_rank_last_checked_at"]
        is None
    )
    sessions.retry_discord.assert_awaited_once_with(
        2,
        error_code="RANK_SNAPSHOT_CONFLICT",
        delay_seconds=5,
        max_attempts=8,
    )
    sessions.fail_discord.assert_not_awaited()
    sessions.complete_discord.assert_not_awaited()
    cog.apply_verified_roles.assert_not_awaited()


@pytest.mark.parametrize("failure_stage", ["roles", "audit", "audit_timeout"])
async def test_rso_discord_retry_reuses_cached_snapshot_without_second_riot_call(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
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
    verifications = _guarded_verifications(
        link,
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
    cog.apply_verified_roles = AsyncMock(
        side_effect=[FakeHTTPException(), None] if failure_stage == "roles" else None
    )
    cog._publish_rso_audit = AsyncMock(
        return_value=True,
        side_effect=(
            [FakeHTTPException(), True]
            if failure_stage == "audit"
            else [TimeoutError(), True]
            if failure_stage == "audit_timeout"
            else None
        ),
    )
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
    assert verifications.retry_rank_role_sync.await_count == (failure_stage == "roles")
    sessions.retry_discord.assert_awaited_once()
    assert cog._publish_rso_audit.await_count == (1 if failure_stage == "roles" else 2)
    member.send.assert_awaited_once()


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
    guild.fetch_member = AsyncMock(return_value=member)
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
    verifications = _guarded_verifications(
        link,
        replacement,
        get_by_user=AsyncMock(
            side_effect=[link, link, replacement, None, None, None, None, None, None]
        ),
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
    cog._publish_rso_audit = AsyncMock(return_value=False)
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


# Launch fixes: worker resilience, rankless completion and durable role retries.


def _completion_cog(
    *,
    claim_pending: AsyncMock,
    complete: AsyncMock,
    retry: AsyncMock,
) -> verification.VerificationCog:
    cog = object.__new__(verification.VerificationCog)
    cog.bot = SimpleNamespace(
        verification_sessions=SimpleNamespace(claim_pending=claim_pending),
    )
    cog._complete_rso_verification = complete
    cog._retry_rso_completion = retry
    return cog


async def test_completion_loop_survives_a_database_error_while_claiming(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claim = AsyncMock(side_effect=ConnectionError("terminating connection"))
    cog = _completion_cog(claim_pending=claim, complete=AsyncMock(), retry=AsyncMock())

    await verification.VerificationCog.complete_rso_verifications.coro(cog)

    claim.assert_awaited_once_with(limit=5, method=None)
    assert "next run will retry" in caplog.text


async def test_one_failed_completion_retry_does_not_abort_the_batch() -> None:
    first = SimpleNamespace(id=1, completion_attempts=1)
    second = SimpleNamespace(id=2, completion_attempts=1)
    complete = AsyncMock(side_effect=[RuntimeError("boom"), None])
    retry = AsyncMock(side_effect=ConnectionError("database is gone"))
    cog = _completion_cog(
        claim_pending=AsyncMock(return_value=[first, second]),
        complete=complete,
        retry=retry,
    )

    await verification.VerificationCog.complete_rso_verifications.coro(cog)

    assert [call.args[0] for call in complete.await_args_list] == [first, second]
    retry.assert_awaited_once_with(first, "UNEXPECTED_COMPLETION_ERROR")


async def test_audit_reconciliation_continues_after_a_failed_retry_write() -> None:
    first = SimpleNamespace(id=1, completion_attempts=1)
    second = SimpleNamespace(id=2, completion_attempts=1)
    cog = object.__new__(verification.VerificationCog)
    cog.bot = SimpleNamespace(
        verification_sessions=SimpleNamespace(
            claim_audit_reconciliations=AsyncMock(return_value=[first, second])
        )
    )
    cog._publish_rso_audit = AsyncMock(side_effect=[RuntimeError("discord"), True])
    cog._retry_rso_completion = AsyncMock(side_effect=ConnectionError("database"))

    await cog._reconcile_rso_audits()

    assert cog._publish_rso_audit.await_count == 2


async def test_riot_monitoring_loop_survives_unexpected_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cog = object.__new__(verification.VerificationCog)
    cog.bot = SimpleNamespace(
        riot_monitor=SimpleNamespace(snapshot=Mock(side_effect=RuntimeError("broken"))),
    )

    await verification.VerificationCog.report_riot_monitoring.coro(cog)

    assert "Riot monitoring report failed" in caplog.text


def _rankless_completion_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    riot_error: Exception,
) -> tuple[verification.VerificationCog, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    member = SimpleNamespace(id=101, send=AsyncMock())
    guild = SimpleNamespace(id=123, name="Moon Poro", get_member=Mock(return_value=member))
    member.guild = guild
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 15, 10, 1, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank=None,
        rank_last_checked_at=None,
    )
    verifications = _guarded_verifications(
        link,
        get_by_user=AsyncMock(return_value=link),
        record_rank_snapshot=AsyncMock(),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        schedule_rank_refresh_now=AsyncMock(return_value=True),
    )
    sessions = SimpleNamespace(retry_discord=AsyncMock(), fail_discord=AsyncMock())
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            rank_refresh_retry_base_seconds=300,
            zweryfikowani_channel_id=None,
        ),
        verifications=verifications,
        verification_sessions=sessions,
        get_guild=Mock(return_value=guild),
    )
    cog = object.__new__(verification.VerificationCog)
    cog.bot = bot
    cog.apply_verified_roles = AsyncMock()
    cog._publish_rso_audit = AsyncMock(return_value=True)
    monkeypatch.setattr(verification, "_get_leagues", AsyncMock(side_effect=riot_error))
    record = SimpleNamespace(
        id=1,
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        completion_attempts=8,
        created_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )
    return cog, record, verifications, member


@pytest.mark.parametrize(
    "riot_error",
    [
        verification.RiotAPIUnavailable(status=503),
        verification.RiotAPIUnavailable(status=None),
        RiotAPINotFound(status=404),
    ],
)
async def test_riot_outage_completes_verification_without_a_rank(
    monkeypatch: pytest.MonkeyPatch,
    riot_error: Exception,
) -> None:
    cog, record, verifications, member = _rankless_completion_setup(
        monkeypatch, riot_error=riot_error
    )

    await cog._complete_rso_verification(record)

    # Verified, member and region roles only: no rank and never Unranked.
    cog.apply_verified_roles.assert_awaited_once_with(member, "EUN1", None)
    guard = verifications.run_verification_role_update_with_identity.await_args.kwargs
    assert guard["expected_rank_last_checked_at"] is None
    verifications.record_rank_snapshot.assert_not_awaited()
    cog.bot.verification_sessions.retry_discord.assert_not_awaited()
    verifications.schedule_rank_refresh_now.assert_awaited_once_with(123, 101)
    cog._publish_rso_audit.assert_awaited_once_with(record)
    message = member.send.await_args.args[0]
    assert "rolę rangi bot doda" in message
    assert "\u2014" not in message and "\u2013" not in message


async def test_rankless_completion_keeps_the_snapshot_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, record, verifications, _member = _rankless_completion_setup(
        monkeypatch, riot_error=verification.RiotAPIUnavailable(status=503)
    )
    verifications.run_verification_role_update_with_identity.side_effect = (
        verification.VerificationSnapshotChanged("newer snapshot")
    )
    cog._retry_rso_completion = AsyncMock()

    await cog._complete_rso_verification(record)

    cog._retry_rso_completion.assert_awaited_once_with(record, "RANK_SNAPSHOT_CONFLICT")
    verifications.schedule_rank_refresh_now.assert_not_awaited()
    cog._publish_rso_audit.assert_not_awaited()


async def test_completion_role_timeout_uses_the_discord_retry_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog, record, verifications, _member = _rankless_completion_setup(
        monkeypatch, riot_error=verification.RiotAPIUnavailable(status=503)
    )
    verifications.run_verification_role_update_with_identity.side_effect = TimeoutError()
    cog._retry_rso_completion = AsyncMock()

    await cog._complete_rso_verification(record)

    verifications.retry_rank_role_sync.assert_awaited_once()
    cog._retry_rso_completion.assert_awaited_once_with(record, "DISCORD_ROLES_UNAVAILABLE")


def _role_sync_link(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "guild_id": 123,
        "discord_user_id": 101,
        "platform": "EUN1",
        "puuid": "puuid",
        "created_at": datetime(2026, 8, 14, tzinfo=UTC),
        "deletion_requested_at": None,
        "last_known_rank": None,
        "rank_last_checked_at": None,
        "rank_role_sync_pending": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_role_sync_worker_restores_rankless_roles_then_requests_the_rank() -> None:
    member = SimpleNamespace(id=101)
    link = _role_sync_link()
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    verifications = _guarded_verifications(
        link,
        claim_due_rank_role_syncs=AsyncMock(return_value=[link]),
        acknowledge_rank_role_sync=AsyncMock(return_value=True),
        retry_rank_role_sync=AsyncMock(),
        schedule_rank_refresh_now=AsyncMock(),
        get_by_user=AsyncMock(return_value=link),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())

    await _retry_next_rank_role_sync(cog)

    cog.apply_verified_roles.assert_awaited_once_with(member, "EUN1", None)
    verifications.acknowledge_rank_role_sync.assert_awaited_once()
    verifications.schedule_rank_refresh_now.assert_awaited_once_with(123, 101)


async def test_role_sync_worker_retries_a_timed_out_role_write() -> None:
    member = SimpleNamespace(id=101)
    link = _role_sync_link(
        last_known_rank="GOLD", rank_last_checked_at=datetime(2026, 8, 15, tzinfo=UTC)
    )
    guild = SimpleNamespace(id=123, get_member=Mock(return_value=member))
    member.guild = guild
    verifications = SimpleNamespace(
        claim_due_rank_role_syncs=AsyncMock(return_value=[link]),
        run_verification_role_update_with_identity=AsyncMock(side_effect=TimeoutError()),
        acknowledge_rank_role_sync=AsyncMock(),
        retry_rank_role_sync=AsyncMock(return_value=300),
    )
    bot = SimpleNamespace(
        settings=_rank_refresh_settings(),
        verifications=verifications,
        get_guild=Mock(return_value=guild),
    )
    cog = SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock())

    await _retry_next_rank_role_sync(cog)

    verifications.retry_rank_role_sync.assert_awaited_once()
    assert (
        verifications.retry_rank_role_sync.await_args.kwargs["expected_rank_last_checked_at"]
        == link.rank_last_checked_at
    )
    verifications.acknowledge_rank_role_sync.assert_not_awaited()


def _join_cog(link: SimpleNamespace, **verification_overrides: object) -> SimpleNamespace:
    attributes: dict[str, object] = {
        "get_by_user": AsyncMock(return_value=link),
        "acknowledge_rank_role_sync": AsyncMock(return_value=True),
        "retry_rank_role_sync": AsyncMock(return_value=300),
        "request_rank_refresh": AsyncMock(),
        "schedule_rank_refresh_now": AsyncMock(),
    }
    attributes.update(verification_overrides)
    verifications = _guarded_verifications(link, **attributes)
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            rank_refresh_retry_base_seconds=300,
            rank_refresh_manual_priority_cooldown_seconds=3600,
        ),
        verifications=verifications,
    )
    return SimpleNamespace(bot=bot, apply_verified_roles=AsyncMock(), _managed_role_updates=set())


async def test_rejoin_without_cached_rank_restores_rankless_roles_and_requests_rank() -> None:
    link = _role_sync_link(rank_role_sync_pending=False)
    cog = _join_cog(link)
    member = SimpleNamespace(id=101, guild=SimpleNamespace(id=123))

    await verification._restore_cached_roles_on_join(cog, member)

    cog.apply_verified_roles.assert_awaited_once_with(member, "EUN1", None)
    cog.bot.verifications.request_rank_refresh.assert_awaited_once_with(
        123,
        101,
        cooldown_seconds=3600,
        source="role_tamper",
    )


@pytest.mark.parametrize("cached_rank", [None, "GOLD"])
async def test_rejoin_role_failure_schedules_the_durable_role_retry(
    monkeypatch: pytest.MonkeyPatch,
    cached_rank: str | None,
) -> None:
    class FakeHTTPException(Exception):
        pass

    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    checked_at = datetime(2026, 8, 15, tzinfo=UTC) if cached_rank else None
    link = _role_sync_link(last_known_rank=cached_rank, rank_last_checked_at=checked_at)
    cog = _join_cog(link)
    cog.apply_verified_roles.side_effect = FakeHTTPException()
    member = SimpleNamespace(id=101, guild=SimpleNamespace(id=123))

    await verification._restore_cached_roles_on_join(cog, member)

    cog.bot.verifications.retry_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        base_delay_seconds=300,
        expected_rank_last_checked_at=checked_at,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=link.created_at,
    )
    cog.bot.verifications.acknowledge_rank_role_sync.assert_not_awaited()


@pytest.mark.parametrize("cached_rank", [None, "GOLD"])
async def test_role_tamper_restore_failure_schedules_the_durable_role_retry(
    monkeypatch: pytest.MonkeyPatch,
    cached_rank: str | None,
) -> None:
    class FakeHTTPException(Exception):
        pass

    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    checked_at = datetime(2026, 8, 15, tzinfo=UTC) if cached_rank else None
    link = _role_sync_link(last_known_rank=cached_rank, rank_last_checked_at=checked_at)
    cog = _join_cog(link)
    cog.apply_verified_roles.side_effect = FakeHTTPException()
    cog.bot.settings.lol_ranks = ["Gold"]
    cog.bot.settings.lol_servers = ["EUNE"]
    cog.bot.settings.verified_role_name = "Verified"
    cog.bot.settings.member_role_name = "Member"
    cog.bot.settings.no_lol_role_name = None
    cog.bot.settings.role_ids = {}
    guild = SimpleNamespace(id=123)
    verified = FakeRole("Verified")
    verified.id = 1  # type: ignore[attr-defined]
    before = SimpleNamespace(id=101, guild=guild, roles=[verified])
    after = SimpleNamespace(id=101, guild=guild, roles=[])

    await verification._reconcile_cached_role_change(cog, before, after)

    cog.bot.verifications.retry_rank_role_sync.assert_awaited_once()
    assert (
        cog.bot.verifications.retry_rank_role_sync.await_args.kwargs[
            "expected_rank_last_checked_at"
        ]
        == checked_at
    )


async def test_missing_configured_roles_are_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(verification, "_reported_missing_roles", set())
    settings = SimpleNamespace(
        lol_ranks=["Gold"],
        lol_servers=["EUNE"],
        verified_role_name="Verified",
        member_role_name="Member",
        role_ids={"EUNE": 999},
    )
    bot = SimpleNamespace(settings=settings)
    guild = SimpleNamespace(id=123, roles=[], get_role=Mock(return_value=None))
    member = SimpleNamespace(
        id=101,
        guild=guild,
        roles=[],
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    guild.fetch_member = AsyncMock(return_value=member)
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}]

    await _apply_verified_roles(bot, member, "EUN1", leagues)
    await _apply_verified_roles(bot, member, "EUN1", leagues)

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 4
    for role_name in ("Gold", "EUNE", "Verified", "Member"):
        assert sum(repr(role_name) in message for message in warnings) == 1
    member.add_roles.assert_not_awaited()


async def test_rso_start_clears_an_orphan_verified_role_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = Mock(spec=verification.discord.Member)
    member.id = 101
    member.roles = [FakeRole("Verified")]
    guild = SimpleNamespace(id=123, name="Moon Poro")
    member.guild = guild
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
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
    lock_held: list[bool] = []

    async def remove_marker(*_args: object, **_kwargs: object) -> None:
        lock_held.append((123, 101) in member_role_lock_module._entries)

    remove = AsyncMock(side_effect=remove_marker)
    monkeypatch.setattr(verification, "_remove_verified_marker", remove)
    interaction = SimpleNamespace(
        user=member,
        guild=guild,
        guild_id=123,
        response=response,
        edit_original_response=AsyncMock(),
    )

    await VerificationStartView(bot).begin_verification(interaction)

    remove.assert_awaited_once()
    assert lock_held == [True]
    # The link is checked once before and once inside the member role lock.
    assert bot.verifications.get_by_user.await_count == 2
    bot.verification_sessions.create.assert_awaited_once()
    assert interaction.edit_original_response.await_args.kwargs["embed"].title == (
        "Połącz konto Riot"
    )


async def test_rso_start_keeps_the_role_when_a_link_appears_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = Mock(spec=verification.discord.Member)
    member.id = 101
    member.roles = [FakeRole("Verified")]
    guild = SimpleNamespace(id=123, name="Moon Poro")
    member.guild = guild
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_cooldown=30,
            verified_role_name="Verified",
            role_ids={},
        ),
        verifications=SimpleNamespace(
            get_by_user=AsyncMock(side_effect=[None, SimpleNamespace(puuid="new")])
        ),
        verification_sessions=SimpleNamespace(create=AsyncMock()),
    )
    remove = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove)
    interaction = SimpleNamespace(
        user=member,
        guild=guild,
        guild_id=123,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await VerificationStartView(bot).begin_verification(interaction)

    remove.assert_not_awaited()
    bot.verification_sessions.create.assert_not_awaited()
    assert (
        "Masz już połączone konto Riot"
        in (interaction.edit_original_response.await_args.kwargs["content"])
    )


def _lookup_cog(account_call: AsyncMock) -> verification.VerificationCog:
    cog = object.__new__(verification.VerificationCog)
    link = SimpleNamespace(discord_user_id=101, puuid="puuid", platform="EUN1")
    cog.bot = SimpleNamespace(
        verifications=SimpleNamespace(
            get_by_user=AsyncMock(return_value=link),
            log_access=AsyncMock(),
        ),
        riot_client=SimpleNamespace(get_account_v1_by_puuid=account_call),
    )
    return cog


@pytest.mark.parametrize(
    ("account_call", "expected_fragment"),
    [
        (AsyncMock(side_effect=RiotAPINotFound(status=404)), "Riot nie znalazł konta"),
        (AsyncMock(return_value={"puuid": "puuid"}), "Riot nie zwrócił Riot ID"),
        (AsyncMock(return_value={"gameName": "Moon_*", "tagLine": "EUNE"}), r"Moon\_\*#EUNE"),
    ],
)
async def test_show_wer_discord_handles_missing_accounts_and_fields(
    account_call: AsyncMock,
    expected_fragment: str,
) -> None:
    cog = _lookup_cog(account_call)
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=900),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await verification.VerificationCog.lookup_by_discord.callback(
        cog, interaction, SimpleNamespace(id=101), "Moderacja"
    )

    message = interaction.followup.send.await_args.args[0]
    assert expected_fragment in message
    assert "chwilowo niedostępny" not in message
    assert "rejestrze wyszukiwań" in message
    cog.bot.verifications.log_access.assert_awaited_once()
