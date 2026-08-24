from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord
import pytest

from moon_poro.cogs import verification, verification_legacy
from moon_poro.cogs.verification_legacy import (
    LegacyIconConfirmationView,
    LegacyVerificationCog,
    LegacyVerificationModal,
    LegacyVerificationRateLimiter,
    LegacyVerificationRetryView,
    LegacyVerificationStartView,
    _normalize_riot_id_parts,
    _riot_id_validation_error,
)
from moon_poro.rank_refresh import RankSnapshot
from moon_poro.riot import RiotAPIUnavailable, RiotAuthBreaker


class FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name


async def _run_guarded_discord_operation(
    *_args: object,
    operation: Callable[[], Awaitable[None]],
    **_kwargs: object,
) -> bool:
    await operation()
    return True


def _guarded_verifications(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "run_verification_role_update_with_identity": AsyncMock(
            side_effect=_run_guarded_discord_operation
        ),
        "run_verification_deletion_role_cleanup_with_identity": AsyncMock(
            side_effect=_run_guarded_discord_operation
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _modal_bot() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_timeout=120,
            verification_cooldown=30,
            verification_icon_check_cooldown=5,
            view_timeout=180,
        )
    )


def _rate_limiter(
    *,
    global_rate: int = 4,
    global_period_seconds: float = 10,
    clock: Callable[[], float] | None = None,
) -> LegacyVerificationRateLimiter:
    if clock is None:
        return LegacyVerificationRateLimiter(
            global_rate=global_rate,
            global_period_seconds=global_period_seconds,
        )
    return LegacyVerificationRateLimiter(
        global_rate=global_rate,
        global_period_seconds=global_period_seconds,
        clock=clock,
    )


def test_riot_id_input_normalizes_whitespace_and_optional_hash() -> None:
    assert _normalize_riot_id_parts(" Moon Poro ", " #EUNE ") == ("Moon Poro", "EUNE")


@pytest.mark.parametrize(
    ("game_name", "tag_line", "expected_fragment"),
    [
        ("ab", "EUNE", "3 do 16"),
        ("Moon#Poro", "EUNE", "przed znakiem"),
        ("Moon Poro", "EU", "3 do 5"),
        ("Moon Poro", "EU-NE", "litery i cyfry"),
    ],
)
def test_riot_id_input_rejects_invalid_parts(
    game_name: str, tag_line: str, expected_fragment: str
) -> None:
    assert expected_fragment in (_riot_id_validation_error(game_name, tag_line) or "")


def test_riot_id_input_accepts_documented_lengths_and_unicode() -> None:
    assert _riot_id_validation_error("Poro", "ABC") is None
    assert _riot_id_validation_error("Księżycowy Poro", "ŁÓDŹ") is None


def test_legacy_modal_uses_constrained_inputs_and_region_select() -> None:
    modal = LegacyVerificationModal(_modal_bot(), _rate_limiter())

    assert modal.game_name.min_length == 3
    assert modal.game_name.max_length == 16
    assert modal.tag_line.min_length == 3
    assert modal.tag_line.max_length == 6
    assert modal.platform.required
    assert [option.value for option in modal.platform.options] == ["EUN1", "EUW1", "NA1"]
    components = modal.to_components()
    assert [component["type"] for component in components] == [
        discord.ComponentType.label.value,
        discord.ComponentType.label.value,
        discord.ComponentType.label.value,
    ]
    assert components[2]["component"]["type"] == discord.ComponentType.string_select.value
    assert modal.title == "Weryfikacja konta Riot"
    assert [(component["label"], component.get("description")) for component in components] == [
        ("Nazwa", "Część Riot ID przed #"),
        ("Tag", "Część Riot ID po # (3-5 liter lub cyfr)"),
        ("Region", None),
    ]
    assert components[0]["component"]["placeholder"] == "np. Moon Poro"
    assert components[1]["component"]["placeholder"] == "np. EUNE"
    assert components[2]["component"]["placeholder"] == "Wybierz region"


async def test_legacy_modal_passes_normalized_values_and_offers_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _modal_bot()
    modal = LegacyVerificationModal(bot, _rate_limiter())
    modal.game_name._value = " Moon Poro "
    modal.tag_line._value = " #EUNE "
    modal.platform._values = ["EUN1"]
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=101),
        response=response,
        followup=followup,
    )
    account_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(verification_legacy, "_get_account", account_lookup)

    await modal.on_submit(interaction)

    response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    account_lookup.assert_awaited_once_with(bot, "Moon Poro", "EUNE", "EUN1")
    assert isinstance(followup.send.await_args.kwargs["view"], LegacyVerificationRetryView)
    assert followup.send.await_args.args[0] == (
        "Nie znaleziono Riot ID **Moon Poro#EUNE** w regionie **EUNE**.\n"
        "Sprawdź Riot ID z profilu Riot (nie nazwę logowania) oraz wybrany region."
    )


async def test_legacy_start_explains_pending_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    monkeypatch.setattr(verification_legacy.discord, "Member", FakeMember)
    pending_link = SimpleNamespace(deletion_requested_at=datetime.now(UTC))
    bot = _modal_bot()
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=pending_link))
    view = LegacyVerificationStartView(bot, _rate_limiter())
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
    )

    await view.begin_verification(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "Usuwanie poprzedniego powiązania jeszcze trwa. Spróbuj ponownie za chwilę.",
        ephemeral=True,
    )
    interaction.response.send_modal.assert_not_awaited()


async def test_legacy_start_explains_incomplete_previous_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    monkeypatch.setattr(verification_legacy.discord, "Member", FakeMember)
    incomplete_link = SimpleNamespace(puuid=None, deletion_requested_at=None)
    bot = _modal_bot()
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=incomplete_link))
    view = LegacyVerificationStartView(bot, _rate_limiter())
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
    )

    await view.begin_verification(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "Otwórz „Moje konto” i usuń poprzednie powiązanie.",
        ephemeral=True,
    )
    interaction.response.send_modal.assert_not_awaited()


async def test_retry_view_reopens_prefilled_modal() -> None:
    view = LegacyVerificationRetryView(
        _modal_bot(),
        _rate_limiter(),
        owner_id=101,
        game_name="Moon Poro",
        tag_line="EUNE",
        platform="EUN1",
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101),
        response=SimpleNamespace(send_modal=AsyncMock()),
    )

    await view.children[0].callback(interaction)

    assert view.children[0].emoji is None
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, LegacyVerificationModal)
    assert modal.game_name.default == "Moon Poro"
    assert modal.tag_line.default == "EUNE"
    assert [option.value for option in modal.platform.options if option.default] == ["EUN1"]


def test_legacy_rate_limiter_applies_per_user_and_scope() -> None:
    now = [100.0]
    limiter = _rate_limiter(clock=lambda: now[0])

    assert limiter.update_rate_limit("account", 101, user_cooldown_seconds=30) is None
    assert limiter.update_rate_limit("account", 101, user_cooldown_seconds=30) == 30
    assert limiter.update_rate_limit("icon", 101, user_cooldown_seconds=5) is None

    now[0] += 30
    assert limiter.update_rate_limit("account", 101, user_cooldown_seconds=30) is None


def test_legacy_rate_limiter_applies_global_window_without_consuming_denied_attempt() -> None:
    now = [100.0]
    limiter = _rate_limiter(global_rate=2, clock=lambda: now[0])

    assert limiter.update_rate_limit("account", 101, user_cooldown_seconds=30) is None
    assert limiter.update_rate_limit("account", 102, user_cooldown_seconds=30) is None
    assert limiter.update_rate_limit("account", 103, user_cooldown_seconds=30) == 10

    now[0] += 10
    assert limiter.update_rate_limit("account", 103, user_cooldown_seconds=30) is None


async def test_legacy_modal_rate_limit_blocks_riot_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _modal_bot()
    limiter = _rate_limiter()
    assert limiter.update_rate_limit("account", 101, user_cooldown_seconds=30) is None
    modal = LegacyVerificationModal(bot, limiter)
    modal.game_name._value = "Moon Poro"
    modal.tag_line._value = "EUNE"
    modal.platform._values = ["EUN1"]
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=101),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    account_lookup = AsyncMock()
    monkeypatch.setattr(verification_legacy, "_get_account", account_lookup)

    await modal.on_submit(interaction)

    account_lookup.assert_not_awaited()
    response.defer.assert_not_awaited()
    assert "Za dużo prób" in response.send_message.await_args.args[0]
    assert isinstance(response.send_message.await_args.kwargs["view"], LegacyVerificationRetryView)


async def test_legacy_modal_hides_riot_api_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _modal_bot()
    modal = LegacyVerificationModal(bot, _rate_limiter())
    modal.game_name._value = "Moon Poro"
    modal.tag_line._value = "EUNE"
    modal.platform._values = ["EUN1"]
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=101),
        response=response,
        followup=followup,
    )
    monkeypatch.setattr(
        verification_legacy,
        "_get_account",
        AsyncMock(side_effect=RiotAPIUnavailable(status=503)),
    )

    await modal.on_submit(interaction)

    response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    followup.send.assert_awaited_once_with(
        "Riot jest chwilowo niedostępny. Spróbuj ponownie później.",
        ephemeral=True,
    )
    assert "API" not in followup.send.await_args.args[0]


async def test_remove_own_verification_marker_keeps_rank_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = FakeRole(1, "Zweryfikowany")
    rank = FakeRole(2, "Emerald")
    region = FakeRole(3, "EUNE")
    member = SimpleNamespace(
        guild=object(),
        roles=[verified, rank, region],
        remove_roles=AsyncMock(),
    )
    bot = SimpleNamespace(settings=SimpleNamespace(verified_role_name="Zweryfikowany"))
    monkeypatch.setattr(verification, "find_role", lambda *_args: verified)

    await verification._remove_verified_marker(bot, member, reason="test")

    member.remove_roles.assert_awaited_once_with(verified, reason="test")


async def test_legacy_riot_lookups_normalize_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_lookup = AsyncMock(return_value={"puuid": "account-puuid"})
    summoner_lookup = AsyncMock(return_value={"profileIconId": 7})
    bot = SimpleNamespace(
        riot_client=SimpleNamespace(
            get_account_v1_by_riot_id=account_lookup,
            get_lol_summoner_v4_by_puuid=summoner_lookup,
        )
    )

    async def execute(call: Callable[[], Awaitable[object]], *, not_found: object) -> object:
        assert not_found is None
        return await call()

    monkeypatch.setattr(verification_legacy, "riot_api_call", execute)

    account = await verification_legacy._get_account(bot, " Moon Poro ", " #EUNE ", "EUN1")
    summoner = await verification_legacy._get_summoner(bot, "EUN1", "account-puuid")

    assert account == {"puuid": "account-puuid"}
    assert summoner == {"profileIconId": 7}
    account_lookup.assert_awaited_once_with(
        game_name="Moon Poro",
        tag_line="EUNE",
        region=verification_legacy.API_SERVERS["EUN1"],
    )
    summoner_lookup.assert_awaited_once_with(region="EUN1", puuid="account-puuid")


async def test_apply_verified_roles_marks_internal_role_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = SimpleNamespace(id=101)
    bot = SimpleNamespace()
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    apply_roles = AsyncMock()
    monkeypatch.setattr(verification_legacy, "_apply_verified_roles", apply_roles)
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]

    await cog.apply_verified_roles(member, "EUN1", leagues)

    apply_roles.assert_awaited_once_with(bot, member, "EUN1", leagues)
    assert cog._managed_role_updates == set()


async def test_member_join_restores_cached_verified_roles_without_riot_lookup() -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="account-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        last_known_rank="EMERALD",
        rank_last_checked_at=datetime.now(UTC),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=_guarded_verifications(
            get_by_user=AsyncMock(return_value=link),
            schedule_rank_refresh_now=AsyncMock(),
        ),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()
    leagues = [
        {
            "queueType": "RANKED_SOLO_5x5",
            "tier": "EMERALD",
            "rank": None,
            "leaguePoints": None,
            "wins": None,
            "losses": None,
            "inactive": None,
        }
    ]

    await cog.on_member_join(member)

    assert bot.verifications.get_by_user.await_args_list == [call(123, 101), call(123, 101)]
    cog.apply_verified_roles.assert_awaited_once_with(member, "EUN1", leagues)
    bot.verifications.schedule_rank_refresh_now.assert_not_awaited()


async def test_member_rejoin_restores_cache_and_refreshes_when_snapshot_is_stale() -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="account-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        last_known_rank="EMERALD",
        rank_last_checked_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    verifications = _guarded_verifications(
        get_by_user=AsyncMock(return_value=link),
        schedule_rank_refresh_now=AsyncMock(),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=verifications,
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_join(member)

    cog.apply_verified_roles.assert_awaited_once()
    verifications.schedule_rank_refresh_now.assert_awaited_once_with(123, 101)


async def test_member_join_does_not_restore_roles_for_pending_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="account-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=datetime.now(UTC),
        deletion_remove_rank_region_roles=False,
    )
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=_guarded_verifications(get_by_user=AsyncMock(return_value=link)),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_join(member)

    cog.apply_verified_roles.assert_not_awaited()
    remove_marker.assert_awaited_once_with(
        cog.bot,
        member,
        reason="Dokończenie usuwania weryfikacji Riot",
    )


async def test_member_join_cleans_pending_delete_without_legacy_puuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid=None,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=datetime.now(UTC),
        deletion_remove_rank_region_roles=False,
    )
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=_guarded_verifications(get_by_user=AsyncMock(return_value=link)),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_join(member)

    cog.apply_verified_roles.assert_not_awaited()
    remove_marker.assert_awaited_once_with(
        cog.bot,
        member,
        reason="Dokończenie usuwania weryfikacji Riot",
    )


async def test_member_join_removes_all_verification_roles_for_pending_admin_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="account-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=datetime.now(UTC),
        deletion_remove_rank_region_roles=True,
    )
    remove_roles = AsyncMock()
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_roles", remove_roles)
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=_guarded_verifications(get_by_user=AsyncMock(return_value=link)),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_join(member)

    cog.apply_verified_roles.assert_not_awaited()
    remove_roles.assert_awaited_once_with(
        cog.bot,
        member,
        reason="Dokończenie usuwania weryfikacji Riot",
    )
    remove_marker.assert_not_awaited()


async def test_member_join_compensates_when_delete_wins_during_cached_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=123)
    member = SimpleNamespace(id=101, guild=guild)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="account-puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="EMERALD",
        rank_last_checked_at=datetime.now(UTC),
    )
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(guild_id=123),
        verifications=_guarded_verifications(
            get_by_user=AsyncMock(side_effect=[link, None, None]),
            schedule_rank_refresh_now=AsyncMock(),
            enqueue_verified_marker_cleanup=AsyncMock(return_value=1),
            acknowledge_verified_marker_cleanup=AsyncMock(),
            retry_verified_marker_cleanup=AsyncMock(),
        ),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_join(member)

    cog.apply_verified_roles.assert_awaited_once()
    remove_marker.assert_awaited_once_with(
        cog.bot,
        member,
        reason="Anulowanie nieaktualnej synchronizacji weryfikacji Riot",
    )
    cog.bot.verifications.schedule_rank_refresh_now.assert_not_awaited()


async def test_rank_refresh_claims_only_configured_guild_records() -> None:
    guild = SimpleNamespace(id=123)
    verifications = SimpleNamespace(
        claim_due_verification_deletions=AsyncMock(return_value=[]),
        claim_due_rank_refreshes=AsyncMock(return_value=[]),
        claim_due_rank_role_syncs=AsyncMock(return_value=[]),
        claim_due_verified_marker_cleanups=AsyncMock(return_value=[]),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            rank_refresh_claim_timeout_seconds=300,
        ),
        get_guild=Mock(return_value=guild),
        verifications=verifications,
        verification_sessions=SimpleNamespace(
            claim_audit_cleanups=AsyncMock(return_value=[]),
        ),
        riot_auth_breaker=RiotAuthBreaker(),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot

    await LegacyVerificationCog.refresh_verified.coro(cog)

    assert bot.get_guild.call_args_list == [call(123), call(123), call(123), call(123)]
    verifications.claim_due_rank_refreshes.assert_awaited_once_with(
        123,
        limit=1,
        claim_timeout_seconds=300,
    )


async def test_remove_own_verification_deletes_link_and_audit_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    class FakeMessageable:
        def __init__(self) -> None:
            self.delete = AsyncMock()
            self.get_partial_message = Mock(return_value=SimpleNamespace(delete=self.delete))

    member = FakeMember(101)
    channel = FakeMessageable()
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(guild_id=123, user=member, guild=guild, response=response)
    link = SimpleNamespace(message_id=456, audit_channel_id=789)
    bot = SimpleNamespace(
        settings=SimpleNamespace(zweryfikowani_channel_id=789),
        verifications=SimpleNamespace(delete_by_user=AsyncMock(return_value=link)),
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    show_confirmation = AsyncMock()
    monkeypatch.setattr(verification_legacy, "_show_delete_confirmation", show_confirmation)

    await LegacyVerificationCog.remove_own_verification.callback(cog, interaction)

    show_confirmation.assert_awaited_once_with(bot, interaction)
    response.send_message.assert_not_awaited()


async def test_icon_confirmation_explains_pending_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id

    monkeypatch.setattr(verification_legacy.discord, "Member", FakeMember)
    pending_link = SimpleNamespace(deletion_requested_at=datetime.now(UTC))
    verifications = SimpleNamespace(
        get_by_user=AsyncMock(return_value=pending_link),
        get_by_puuid=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_timeout=120,
            verification_icon_check_cooldown=5,
        ),
        verifications=verifications,
    )
    view = LegacyIconConfirmationView(
        bot,
        _rate_limiter(),
        owner_id=101,
        platform="EUN1",
        puuid="puuid",
        game_name="Moon",
        tag_line="EUNE",
        expected_icon_id=42,
    )
    interaction = SimpleNamespace(
        guild=object(),
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    assert view.children[0].emoji is None
    await view.children[0].callback(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once_with(
        "Usuwanie poprzedniego powiązania jeszcze trwa. Spróbuj ponownie za chwilę.",
        ephemeral=True,
    )
    verifications.get_by_puuid.assert_not_awaited()


async def test_legacy_discord_failure_keeps_link_and_enqueues_cached_role_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    class FakeMember:
        def __init__(self, user_id: int, guild: object) -> None:
            self.id = user_id
            self.guild = guild
            self.roles: list[object] = []

    audit_message = SimpleNamespace(id=456, delete=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=audit_message))
    guild = SimpleNamespace(id=123, get_channel=Mock(return_value=channel))
    member = FakeMember(101, guild)
    checked_at = datetime(2026, 8, 15, tzinfo=UTC)
    created_at = datetime(2026, 8, 14, tzinfo=UTC)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
        rank_last_checked_at=checked_at,
    )
    verifications = _guarded_verifications(
        get_by_user=AsyncMock(return_value=None),
        get_by_puuid=AsyncMock(return_value=None),
        create=AsyncMock(return_value=link),
        retry_rank_role_sync=AsyncMock(return_value=300),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_timeout=120,
            verification_icon_check_cooldown=5,
            zweryfikowani_channel_id=789,
            rank_refresh_interval_hours=24,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=verifications,
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog.apply_verified_roles = AsyncMock(side_effect=FakeHTTPException())
    bot.get_cog = Mock(return_value=cog)
    response = SimpleNamespace(defer=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=123,
        user=member,
        response=response,
        followup=followup,
        edit_original_response=AsyncMock(),
    )
    leagues = [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD"}]
    monkeypatch.setattr(verification_legacy.discord, "Member", FakeMember)
    monkeypatch.setattr(verification_legacy.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification_legacy.discord.abc, "Messageable", type(channel))
    monkeypatch.setattr(
        verification_legacy,
        "_get_summoner",
        AsyncMock(return_value={"profileIconId": 42}),
    )
    monkeypatch.setattr(verification_legacy, "_get_leagues", AsyncMock(return_value=leagues))
    view = LegacyIconConfirmationView(
        bot,
        _rate_limiter(),
        owner_id=101,
        platform="EUN1",
        puuid="puuid",
        game_name="Moon",
        tag_line="EUNE",
        expected_icon_id=42,
    )

    await view.children[0].callback(interaction)

    verifications.create.assert_awaited_once()
    assert verifications.create.await_args.kwargs["rank_snapshot"] == RankSnapshot("EMERALD")
    assert verifications.create.await_args.kwargs["audit_channel_id"] == 789
    assert verifications.create.await_args.kwargs["riot_game_name"] == "Moon"
    assert verifications.create.await_args.kwargs["riot_tag_line"] == "EUNE"
    verifications.retry_rank_role_sync.assert_awaited_once_with(
        123,
        101,
        base_delay_seconds=300,
        expected_rank_last_checked_at=checked_at,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=created_at,
    )
    audit_message.delete.assert_not_awaited()
    assert "dokończy nadawanie ról" in followup.send.await_args.args[0]


async def test_manual_rank_change_is_reconciled_from_cached_rank() -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    verified = FakeRole(3, "Zweryfikowany")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald, verified])
    after = SimpleNamespace(id=101, guild=guild, roles=[emerald, iron, verified])
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        last_known_rank="EMERALD",
        rank_last_checked_at=datetime.now(UTC),
    )
    settings = SimpleNamespace(
        guild_id=123,
        lol_ranks=["Iron", "Emerald"],
        lol_servers=["EUNE"],
        verified_role_name="Zweryfikowany",
        member_role_name="Użytkownik",
        rank_refresh_manual_priority_cooldown_seconds=3600,
        role_ids={},
    )
    bot = SimpleNamespace(
        settings=settings,
        verifications=_guarded_verifications(
            get_by_user=AsyncMock(return_value=link),
            request_rank_refresh=AsyncMock(),
        ),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()
    leagues = [
        {
            "queueType": "RANKED_SOLO_5x5",
            "tier": "EMERALD",
            "rank": None,
            "leaguePoints": None,
            "wins": None,
            "losses": None,
            "inactive": None,
        }
    ]

    await cog.on_member_update(before, after)

    assert bot.verifications.get_by_user.await_args_list == [call(123, 101), call(123, 101)]
    cog.apply_verified_roles.assert_awaited_once_with(after, "EUN1", leagues)
    bot.verifications.request_rank_refresh.assert_not_awaited()


async def test_member_update_does_not_restore_roles_for_pending_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald])
    after = SimpleNamespace(id=101, guild=guild, roles=[iron])
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=datetime.now(UTC),
        deletion_remove_rank_region_roles=False,
    )
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            lol_ranks=["Iron", "Emerald"],
            lol_servers=["EUNE"],
            verified_role_name="Zweryfikowany",
            member_role_name="Użytkownik",
            role_ids={},
        ),
        verifications=_guarded_verifications(get_by_user=AsyncMock(return_value=link)),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_update(before, after)

    cog.apply_verified_roles.assert_not_awaited()
    remove_marker.assert_awaited_once_with(
        cog.bot,
        after,
        reason="Dokończenie usuwania weryfikacji Riot",
    )


async def test_member_update_cleans_pending_delete_without_legacy_puuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald])
    after = SimpleNamespace(id=101, guild=guild, roles=[iron])
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid=None,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=datetime.now(UTC),
        deletion_remove_rank_region_roles=False,
    )
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            lol_ranks=["Iron", "Emerald"],
            lol_servers=["EUNE"],
            verified_role_name="Zweryfikowany",
            member_role_name="Użytkownik",
            role_ids={},
        ),
        verifications=_guarded_verifications(get_by_user=AsyncMock(return_value=link)),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_update(before, after)

    cog.apply_verified_roles.assert_not_awaited()
    remove_marker.assert_awaited_once_with(
        cog.bot,
        after,
        reason="Dokończenie usuwania weryfikacji Riot",
    )


async def test_member_update_removes_all_verification_roles_for_pending_admin_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald])
    after = SimpleNamespace(id=101, guild=guild, roles=[iron])
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=datetime.now(UTC),
        deletion_remove_rank_region_roles=True,
    )
    remove_roles = AsyncMock()
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_roles", remove_roles)
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            lol_ranks=["Iron", "Emerald"],
            lol_servers=["EUNE"],
            verified_role_name="Zweryfikowany",
            member_role_name="Użytkownik",
            role_ids={},
        ),
        verifications=_guarded_verifications(get_by_user=AsyncMock(return_value=link)),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_update(before, after)

    cog.apply_verified_roles.assert_not_awaited()
    remove_roles.assert_awaited_once_with(
        cog.bot,
        after,
        reason="Dokończenie usuwania weryfikacji Riot",
    )
    remove_marker.assert_not_awaited()


async def test_member_update_compensates_when_delete_wins_during_cached_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald])
    after = SimpleNamespace(id=101, guild=guild, roles=[iron])
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        deletion_requested_at=None,
        last_known_rank="EMERALD",
        rank_last_checked_at=datetime.now(UTC),
    )
    remove_marker = AsyncMock()
    monkeypatch.setattr(verification, "_remove_verified_marker", remove_marker)
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            lol_ranks=["Iron", "Emerald"],
            lol_servers=["EUNE"],
            verified_role_name="Zweryfikowany",
            member_role_name="Użytkownik",
            rank_refresh_manual_priority_cooldown_seconds=3600,
            role_ids={},
        ),
        verifications=_guarded_verifications(
            get_by_user=AsyncMock(side_effect=[link, None, None]),
            request_rank_refresh=AsyncMock(),
            enqueue_verified_marker_cleanup=AsyncMock(return_value=1),
            acknowledge_verified_marker_cleanup=AsyncMock(),
            retry_verified_marker_cleanup=AsyncMock(),
        ),
    )
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_update(before, after)

    cog.apply_verified_roles.assert_awaited_once()
    remove_marker.assert_awaited_once_with(
        cog.bot,
        after,
        reason="Anulowanie nieaktualnej synchronizacji weryfikacji Riot",
    )
    cog.bot.verifications.request_rank_refresh.assert_not_awaited()


async def test_manual_rank_change_prioritizes_stale_cache_with_durable_cooldown() -> None:
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald])
    after = SimpleNamespace(id=101, guild=guild, roles=[emerald, iron])
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        last_known_rank="EMERALD",
        rank_last_checked_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    settings = SimpleNamespace(
        guild_id=123,
        lol_ranks=["Iron", "Emerald"],
        lol_servers=["EUNE"],
        verified_role_name="Zweryfikowany",
        member_role_name="Użytkownik",
        role_ids={},
        rank_refresh_manual_priority_cooldown_seconds=3600,
    )
    verifications = _guarded_verifications(
        get_by_user=AsyncMock(return_value=link),
        request_rank_refresh=AsyncMock(),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(settings=settings, verifications=verifications)
    cog._managed_role_updates = set()
    cog.apply_verified_roles = AsyncMock()

    await cog.on_member_update(before, after)

    verifications.request_rank_refresh.assert_awaited_once_with(
        123,
        101,
        cooldown_seconds=3600,
        source="role_tamper",
    )


async def test_manual_rank_change_without_cache_restores_previous_roles_and_schedules_refresh() -> (
    None
):
    emerald = FakeRole(1, "Emerald")
    iron = FakeRole(2, "Iron")
    verified = FakeRole(3, "Zweryfikowany")
    guild = SimpleNamespace(id=123)
    before = SimpleNamespace(id=101, guild=guild, roles=[emerald, verified])
    after = SimpleNamespace(
        id=101,
        guild=guild,
        roles=[emerald, iron, verified],
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        platform="EUN1",
        puuid="puuid",
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        last_known_rank=None,
    )
    settings = SimpleNamespace(
        guild_id=123,
        lol_ranks=["Iron", "Emerald"],
        lol_servers=["EUNE"],
        verified_role_name="Zweryfikowany",
        member_role_name="Użytkownik",
        rank_refresh_manual_priority_cooldown_seconds=3600,
        role_ids={},
    )
    verifications = _guarded_verifications(
        get_by_user=AsyncMock(return_value=link),
        request_rank_refresh=AsyncMock(),
    )
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = SimpleNamespace(settings=settings, verifications=verifications)
    cog._managed_role_updates = set()

    await cog.on_member_update(before, after)

    after.remove_roles.assert_awaited_once_with(iron, reason="Ochrona ról weryfikacji Riot")
    after.add_roles.assert_not_awaited()
    verifications.request_rank_refresh.assert_awaited_once_with(
        123,
        101,
        cooldown_seconds=3600,
        source="role_tamper",
    )
    assert cog._managed_role_updates == set()
