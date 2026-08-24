from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from moon_poro.account_profile import build_account_profile
from moon_poro.cogs import verification, verification_legacy
from moon_poro.cogs.verification import (
    AccountProfileView,
    DeleteVerificationConfirmationView,
    VerificationCog,
    VerificationStartView,
    _remove_user_verification,
    _request_rank_refresh_from_panel,
    _show_account_profile,
    _show_delete_confirmation,
)
from moon_poro.cogs.verification_legacy import (
    LegacyVerificationCog,
    LegacyVerificationRateLimiter,
    LegacyVerificationStartView,
)
from moon_poro.repositories import RankRefreshRequestResult, RankRefreshRequestStatus


class FakeMember:
    def __init__(self, user_id: int, *, guild: object | None = None) -> None:
        self.id = user_id
        self.guild = guild
        self.roles: list[object] = []
        self.remove_roles = AsyncMock()


def _panel_bot() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            guild_id=123,
            verification_cooldown=30,
            verification_global_rate_limit=4,
            verification_global_rate_period_seconds=10,
            rank_refresh_button_cooldown_seconds=1800,
            rank_refresh_claim_timeout_seconds=300,
        ),
        riot_auth_breaker=SimpleNamespace(
            snapshot=Mock(return_value=SimpleNamespace(blocked=False))
        ),
    )


def _profile_link(**overrides: object) -> SimpleNamespace:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "platform": "EUN1",
        "puuid": "puuid-101",
        "created_at": now - timedelta(days=30),
        "riot_game_name": "Moon Poro",
        "riot_tag_line": "EUNE",
        "last_known_rank": "EMERALD",
        "last_known_division": "II",
        "last_known_league_points": 42,
        "last_known_wins": 20,
        "last_known_losses": 10,
        "rank_last_checked_at": now - timedelta(hours=1),
        "rank_refresh_claimed_at": None,
        "rank_next_refresh_at": now + timedelta(hours=6),
        "rank_refresh_failures": 0,
        "rank_user_refresh_requested_at": None,
        "deletion_requested_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_rso_and_legacy_views_are_persistent_and_keep_old_start_ids() -> None:
    bot = _panel_bot()
    limiter = LegacyVerificationRateLimiter(global_rate=4, global_period_seconds=10)
    rso_view = VerificationStartView(bot)
    legacy_view = LegacyVerificationStartView(bot, limiter)

    assert rso_view.timeout is None
    assert legacy_view.timeout is None
    assert {item.custom_id for item in rso_view.children} == {
        "verification:start:rso:v1",
        "verification:account-profile:v1",
        "verification:rank-refresh:v1",
        "verification:delete:v1",
    }
    assert {item.custom_id for item in legacy_view.children} == {
        "verification:start:profile-icon:v1",
        "verification:account-profile:v1",
        "verification:rank-refresh:v1",
        "verification:delete:v1",
    }
    assert [item.label for item in rso_view.children] == [
        "Zweryfikuj konto",
        "Moje konto",
        "Odśwież rangę",
        "Usuń powiązanie",
    ]
    assert all(item.emoji is None for item in rso_view.children)
    assert all(item.emoji is None for item in legacy_view.children)


def test_profile_commands_are_guild_only() -> None:
    assert VerificationCog.profile.guild_only is True
    assert LegacyVerificationCog.profile.guild_only is True


async def test_both_panels_open_profile_through_the_shared_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    limiter = LegacyVerificationRateLimiter(global_rate=4, global_period_seconds=10)
    rso_view = VerificationStartView(bot)
    legacy_view = LegacyVerificationStartView(bot, limiter)
    rso_handler = AsyncMock()
    legacy_handler = AsyncMock()
    monkeypatch.setattr(verification, "_show_account_profile", rso_handler)
    monkeypatch.setattr(verification_legacy, "_show_account_profile", legacy_handler)
    interaction = SimpleNamespace()

    rso_button = next(
        item for item in rso_view.children if item.custom_id == "verification:account-profile:v1"
    )
    legacy_button = next(
        item for item in legacy_view.children if item.custom_id == "verification:account-profile:v1"
    )
    await rso_button.callback(interaction)
    await legacy_button.callback(interaction)

    assert rso_handler.await_args.args == (bot, interaction)
    assert rso_handler.await_args.kwargs["start_verification"].__self__ is rso_view
    assert legacy_handler.await_args.args == (bot, interaction)
    assert legacy_handler.await_args.kwargs["start_verification"].__self__ is legacy_view


async def test_profile_command_uses_the_same_handler_as_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    cog.rso_start_view = VerificationStartView(bot)
    handler = AsyncMock()
    monkeypatch.setattr(verification, "_show_account_profile", handler)
    interaction = SimpleNamespace()

    await VerificationCog.profile.callback(cog, interaction)

    assert handler.await_args.args == (bot, interaction)
    assert handler.await_args.kwargs["start_verification"].__self__ is cog.rso_start_view


async def test_rso_persistent_start_rejects_another_guild() -> None:
    bot = _panel_bot()
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(guild_id=999, response=response)

    await VerificationStartView(bot).children[0].callback(interaction)

    response.send_message.assert_awaited_once_with(
        "Weryfikację możesz rozpocząć tylko na serwerze Moon Poro.", ephemeral=True
    )


@pytest.mark.parametrize("provider", ["rso", "legacy"])
async def test_verification_start_cooldown_never_says_zero_seconds(provider: str) -> None:
    bot = _panel_bot()
    if provider == "rso":
        view = VerificationStartView(bot)
    else:
        view = LegacyVerificationStartView(
            bot,
            LegacyVerificationRateLimiter(global_rate=4, global_period_seconds=10),
        )
    view.cooldowns = SimpleNamespace(update_rate_limit=Mock(return_value=0.1))
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(guild_id=123, response=response)

    await view.begin_verification(interaction)

    response.send_message.assert_awaited_once_with(
        "Spróbuj ponownie za 1 s.",
        ephemeral=True,
    )


async def test_rso_start_blocks_reverification_while_delete_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=123)
    member = FakeMember(101, guild=guild)
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    pending_delete = SimpleNamespace(deletion_requested_at=datetime.now(UTC))
    sessions = SimpleNamespace(create=AsyncMock())
    bot = _panel_bot()
    bot.settings.verified_role_name = "Zweryfikowany"
    bot.settings.role_ids = {}
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=pending_delete))
    bot.verification_sessions = sessions
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        guild=guild,
        user=member,
        response=response,
    )

    await VerificationStartView(bot).children[0].callback(interaction)

    assert response.send_message.await_args.args[0] == (
        "Usuwanie poprzedniego powiązania jeszcze trwa. Spróbuj ponownie za chwilę."
    )
    sessions.create.assert_not_awaited()


async def test_rso_publishes_short_factual_embed() -> None:
    bot = _panel_bot()
    bot.settings.privacy_policy_url = "https://moonporo.pl/privacy/"
    bot.settings.rso_base_url = "https://moonporo.pl"
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    cog.rso_start_view = VerificationStartView(bot)
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

    await VerificationCog.publish_verification.callback(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["embed"].title == "Weryfikacja konta League of Legends"
    assert "oficjalne logowanie Riot" in kwargs["embed"].description
    assert "dywiz" not in kwargs["embed"].description.lower()
    assert isinstance(kwargs["view"], VerificationStartView)


async def test_legacy_publishes_icon_verification_embed() -> None:
    bot = _panel_bot()
    cog = object.__new__(LegacyVerificationCog)
    cog.bot = bot
    cog.rate_limiter = LegacyVerificationRateLimiter(global_rate=4, global_period_seconds=10)
    cog.legacy_start_view = LegacyVerificationStartView(bot, cog.rate_limiter)
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

    await LegacyVerificationCog.publish_verification.callback(cog, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["embed"].title == "Weryfikacja konta League of Legends"
    assert "ikonę profilu" in kwargs["embed"].description
    assert "rangi Solo/Duo" in kwargs["embed"].fields[0].value
    assert isinstance(kwargs["view"], LegacyVerificationStartView)


async def test_profile_is_ephemeral_and_owner_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    link = _profile_link()
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=link))
    starter = AsyncMock()
    member = FakeMember(101)
    interaction = SimpleNamespace(
        guild_id=123,
        user=member,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    await _show_account_profile(
        bot,
        interaction,
        start_verification=starter,
    )

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    bot.verifications.get_by_user.assert_awaited_once_with(123, 101)
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert kwargs["embed"].title == "Moje konto"
    assert kwargs["embed"].fields[0].value == "Moon Poro#EUNE"
    view = kwargs["view"]
    assert isinstance(view, AccountProfileView)
    assert view.owner_id == 101
    assert view.timeout == 2100
    assert [item.label for item in view.children] == [
        "Odśwież rangę",
        "Usuń powiązanie",
    ]
    assert all(item.emoji is None for item in view.children)

    foreign_interaction = SimpleNamespace(
        user=SimpleNamespace(id=202),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    assert await view.interaction_check(foreign_interaction) is False
    foreign_interaction.response.send_message.assert_awaited_once_with(
        "Nie możesz użyć przycisków w profilu innej osoby.", ephemeral=True
    )


async def test_unverified_profile_only_reuses_active_verification_provider() -> None:
    bot = _panel_bot()
    starter = AsyncMock()
    presentation = build_account_profile(None)
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=presentation,
        link=None,
        start_verification=starter,
    )

    assert [item.label for item in view.children] == ["Zweryfikuj konto"]
    assert view.children[0].emoji is None
    interaction = SimpleNamespace()
    await view.children[0].callback(interaction)

    starter.assert_awaited_once_with(interaction)


async def test_profile_refresh_shows_queued_running_and_completed_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS", 0.0)
    bot = _panel_bot()
    original = _profile_link()
    requested_at = datetime.now(UTC)
    locked_baseline = requested_at - timedelta(minutes=1)
    queued = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=locked_baseline,
        rank_next_refresh_at=requested_at - timedelta(seconds=1),
        rank_user_refresh_requested_at=requested_at,
    )
    running = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=locked_baseline,
        rank_refresh_claimed_at=requested_at,
        rank_next_refresh_at=requested_at - timedelta(seconds=1),
        rank_user_refresh_requested_at=requested_at,
    )
    completed = _profile_link(
        created_at=original.created_at,
        last_known_rank="DIAMOND",
        last_known_division="IV",
        last_known_league_points=73,
        rank_last_checked_at=requested_at + timedelta(seconds=1),
        rank_next_refresh_at=requested_at + timedelta(hours=6),
        rank_user_refresh_requested_at=requested_at,
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=locked_baseline,
            )
        ),
        get_by_user=AsyncMock(side_effect=[queued, running, completed]),
    )
    starter = AsyncMock()
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=starter,
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    interaction.response.defer.assert_awaited_once_with()
    bot.verifications.request_rank_refresh.assert_awaited_once_with(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
        expected_puuid=original.puuid,
        expected_platform=original.platform,
        expected_created_at=original.created_at,
    )
    assert bot.verifications.get_by_user.await_count == 3
    assert all(call.args == (123, 101) for call in bot.verifications.get_by_user.await_args_list)
    assert interaction.edit_original_response.await_count == 3

    queued_kwargs = interaction.edit_original_response.await_args_list[0].kwargs
    assert queued_kwargs["embed"].description is None
    assert isinstance(queued_kwargs["view"], AccountProfileView)
    assert queued_kwargs["view"].children[0].label == "W kolejce…"
    assert queued_kwargs["view"].children[0].disabled is True
    assert queued_kwargs["view"].children[0].style.name == "secondary"

    running_kwargs = interaction.edit_original_response.await_args_list[1].kwargs
    assert running_kwargs["embed"].description is None
    assert running_kwargs["view"].children[0].label == "Odświeżanie…"
    assert running_kwargs["view"].children[0].disabled is True

    completed_kwargs = interaction.edit_original_response.await_args_list[2].kwargs
    assert completed_kwargs["embed"].description is None
    assert completed_kwargs["embed"].footer.text is None
    fields = {field.name: field.value for field in completed_kwargs["embed"].fields}
    assert fields["Solo/Duo"] == "Diamond IV"
    assert fields["LP"] == "73 LP"
    assert completed_kwargs["view"].children[0].label == "Odśwież rangę"
    assert completed_kwargs["view"].children[0].disabled is False


async def test_profile_refresh_detects_completion_before_first_poll() -> None:
    bot = _panel_bot()
    original = _profile_link()
    completed = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=datetime.now(UTC) + timedelta(seconds=1),
        rank_user_refresh_requested_at=datetime.now(UTC),
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ALREADY_CLAIMED,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(return_value=completed),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    bot.verifications.get_by_user.assert_awaited_once_with(123, 101)
    interaction.edit_original_response.assert_awaited_once()
    completed_embed = interaction.edit_original_response.await_args.kwargs["embed"]
    assert completed_embed.description is None
    assert completed_embed.footer.text is None


async def test_profile_refresh_timeout_keeps_queue_running_and_button_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "_ACCOUNT_PROFILE_REFRESH_WATCH_TIMEOUT_SECONDS", 0.0)
    bot = _panel_bot()
    original = _profile_link()
    queued = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=original.rank_last_checked_at,
        rank_next_refresh_at=datetime.now(UTC) - timedelta(seconds=1),
        rank_user_refresh_requested_at=datetime.now(UTC),
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(return_value=queued),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    bot.verifications.request_rank_refresh.assert_awaited_once()
    assert bot.verifications.get_by_user.await_count == 2
    timeout_kwargs = interaction.edit_original_response.await_args.kwargs
    timeout_embed = timeout_kwargs["embed"]
    assert timeout_embed.description is None
    assert timeout_embed.fields[-1].name == "Stan odświeżania"
    assert timeout_embed.fields[-1].value == (
        "Odświeżanie trwa dłużej niż zwykle.\nOtwórz `/profil` ponownie za chwilę."
    )
    assert timeout_kwargs["view"].children[0].disabled is True
    assert timeout_kwargs["view"].children[0].label == "W kolejce…"


@pytest.mark.parametrize("final_state", ["completed", "temporary_failure"])
async def test_profile_refresh_timeout_final_read_prefers_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    final_state: str,
) -> None:
    monkeypatch.setattr(verification, "_ACCOUNT_PROFILE_REFRESH_WATCH_TIMEOUT_SECONDS", 0.0)
    bot = _panel_bot()
    original = _profile_link()
    queued = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=original.rank_last_checked_at,
        rank_next_refresh_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    final_link = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=(
            datetime.now(UTC) + timedelta(seconds=1)
            if final_state == "completed"
            else original.rank_last_checked_at
        ),
        rank_next_refresh_at=datetime.now(UTC) + timedelta(hours=6),
        rank_refresh_failures=1 if final_state == "temporary_failure" else 0,
        rank_user_refresh_requested_at=datetime.now(UTC),
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(side_effect=[queued, final_link]),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    assert bot.verifications.get_by_user.await_count == 2
    final_embed = interaction.edit_original_response.await_args.kwargs["embed"]
    assert final_embed.description is None
    if final_state == "completed":
        assert final_embed.footer.text is None
        assert all(field.name != "Stan odświeżania" for field in final_embed.fields)
    else:
        assert final_embed.fields[-1].name == "Stan odświeżania"
        assert final_embed.fields[-1].value == (
            "Riot jest chwilowo niedostępny.\nSpróbujemy ponownie automatycznie."
        )
    assert "trwa dłużej" not in str(final_embed.to_dict())


async def test_profile_refresh_stops_watching_after_temporary_riot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS", 0.0)
    bot = _panel_bot()
    original = _profile_link()
    requested_at = datetime.now(UTC)
    queued = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=original.rank_last_checked_at,
        rank_next_refresh_at=requested_at - timedelta(seconds=1),
        rank_user_refresh_requested_at=requested_at,
    )
    failed = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=original.rank_last_checked_at,
        rank_refresh_failures=1,
        rank_next_refresh_at=requested_at + timedelta(minutes=1),
        rank_user_refresh_requested_at=requested_at,
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(side_effect=[queued, failed]),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    assert bot.verifications.get_by_user.await_count == 2
    failure_kwargs = interaction.edit_original_response.await_args.kwargs
    failure_embed = failure_kwargs["embed"]
    assert failure_embed.description is None
    assert failure_embed.fields[-1].name == "Stan odświeżania"
    assert failure_embed.fields[-1].value == (
        "Riot jest chwilowo niedostępny.\nSpróbujemy ponownie automatycznie."
    )
    assert failure_kwargs["view"].children[0].label == "Odśwież rangę"
    assert failure_kwargs["view"].children[0].disabled is True


@pytest.mark.parametrize(
    "status",
    [RankRefreshRequestStatus.LINK_CHANGED, RankRefreshRequestStatus.NOT_LINKED],
)
async def test_stale_profile_refresh_does_not_render_the_new_link(
    status: RankRefreshRequestStatus,
) -> None:
    bot = _panel_bot()
    original = _profile_link()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(return_value=RankRefreshRequestResult(status)),
        get_by_user=AsyncMock(),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    bot.verifications.get_by_user.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once_with(
        content="Ten widok jest już nieaktualny. Otwórz `/profil` ponownie.",
        embed=None,
        view=None,
    )


@pytest.mark.parametrize("link_change", ["reverified", "deleting"])
async def test_profile_refresh_stops_if_link_changes_while_watching(
    monkeypatch: pytest.MonkeyPatch,
    link_change: str,
) -> None:
    monkeypatch.setattr(verification, "_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS", 0.0)
    bot = _panel_bot()
    original = _profile_link()
    queued = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=original.rank_last_checked_at,
        rank_next_refresh_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    replacement = (
        _profile_link(puuid="replacement-puuid")
        if link_change == "reverified"
        else _profile_link(
            created_at=original.created_at,
            rank_last_checked_at=original.rank_last_checked_at,
            deletion_requested_at=datetime.now(UTC),
        )
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(side_effect=[queued, replacement]),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    assert interaction.edit_original_response.await_args.kwargs == {
        "content": "Ten widok jest już nieaktualny. Otwórz `/profil` ponownie.",
        "embed": None,
        "view": None,
    }


async def test_same_profile_view_rejects_a_second_refresh_callback() -> None:
    bot = _panel_bot()
    original = _profile_link()
    bot.verifications = SimpleNamespace(request_rank_refresh=AsyncMock())
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    view._refresh_in_progress = True
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    bot.verifications.request_rank_refresh.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Sprawdzamy możliwość odświeżenia. Poczekaj chwilę.",
        ephemeral=True,
    )


async def test_profile_refresh_hides_internal_error_and_allows_retry() -> None:
    bot = _panel_bot()
    original = _profile_link()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(side_effect=RuntimeError("internal database details"))
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    interaction.edit_original_response.assert_awaited_once()
    error_kwargs = interaction.edit_original_response.await_args.kwargs
    assert error_kwargs["content"] is None
    assert error_kwargs["embed"].description is None
    assert error_kwargs["embed"].fields[-1].name == "Stan odświeżania"
    assert error_kwargs["embed"].fields[-1].value == (
        "Nie udało się rozpocząć odświeżania.\nSpróbuj ponownie."
    )
    assert "internal" not in error_kwargs["embed"].fields[-1].value
    assert error_kwargs["view"] is view
    assert refresh.label == "Odśwież rangę"
    assert refresh.disabled is False
    assert view._refresh_in_progress is False


@pytest.mark.parametrize(
    ("retry_after_seconds", "expected_message"),
    [
        (1201, "Rangę możesz odświeżyć za około 21 min."),
        (59, "Rangę możesz odświeżyć za 59 s."),
        (None, "Rangę będzie można odświeżyć za chwilę."),
        (0, "Rangę będzie można odświeżyć za chwilę."),
        (-1, "Rangę będzie można odświeżyć za chwilę."),
    ],
)
async def test_profile_refresh_cooldown_uses_followup_without_rerendering(
    retry_after_seconds: int | None,
    expected_message: str,
) -> None:
    bot = _panel_bot()
    original = _profile_link(rank_user_refresh_requested_at=datetime.now(UTC))
    get_by_user = AsyncMock(return_value=original)
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.COOLDOWN,
                retry_after_seconds=retry_after_seconds,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=get_by_user,
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    assert refresh.disabled is False

    await refresh.callback(interaction)

    interaction.response.defer.assert_awaited_once_with()
    get_by_user.assert_awaited_once_with(123, 101)
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with(expected_message, ephemeral=True)
    assert refresh.label == "Odśwież rangę"
    assert refresh.disabled is False
    assert refresh.style.name == "primary"
    assert view._refresh_in_progress is False


@pytest.mark.parametrize(
    ("pending_state", "expected_label"),
    [
        ("queued", "W kolejce…"),
        ("running", "Odświeżanie…"),
    ],
)
async def test_second_open_profile_follows_refresh_hidden_by_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    pending_state: str,
    expected_label: str,
) -> None:
    monkeypatch.setattr(verification, "_ACCOUNT_PROFILE_REFRESH_POLL_INTERVAL_SECONDS", 0.0)
    bot = _panel_bot()
    original = _profile_link()
    requested_at = datetime.now(UTC)
    pending_overrides: dict[str, object] = {
        "created_at": original.created_at,
        "rank_last_checked_at": original.rank_last_checked_at,
        "rank_next_refresh_at": requested_at - timedelta(seconds=1),
        "rank_user_refresh_requested_at": requested_at,
    }
    if pending_state == "running":
        pending_overrides["rank_refresh_claimed_at"] = requested_at
    pending = _profile_link(**pending_overrides)
    completed = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=requested_at + timedelta(seconds=1),
        rank_user_refresh_requested_at=requested_at,
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.COOLDOWN,
                retry_after_seconds=1200,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(side_effect=[pending, completed]),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    assert bot.verifications.get_by_user.await_count == 2
    assert interaction.edit_original_response.await_count == 2
    pending_view = interaction.edit_original_response.await_args_list[0].kwargs["view"]
    assert pending_view.children[0].label == expected_label
    assert pending_view.children[0].disabled is True
    completed_view = interaction.edit_original_response.await_args_list[1].kwargs["view"]
    assert completed_view.children[0].label == "Odśwież rangę"
    assert completed_view.children[0].disabled is False
    interaction.followup.send.assert_not_awaited()


async def test_profile_cooldown_renders_a_terminal_riot_failure() -> None:
    bot = _panel_bot()
    original = _profile_link()
    failed = _profile_link(
        created_at=original.created_at,
        rank_last_checked_at=original.rank_last_checked_at,
        rank_refresh_failures=1,
        rank_user_refresh_requested_at=datetime.now(UTC),
    )
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.COOLDOWN,
                retry_after_seconds=1200,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(return_value=failed),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    interaction.edit_original_response.assert_awaited_once()
    rendered = interaction.edit_original_response.await_args.kwargs
    assert rendered["embed"].fields[-1].value.startswith("Riot jest chwilowo niedostępny.")
    assert rendered["view"].children[0].disabled is True
    interaction.followup.send.assert_not_awaited()


async def test_profile_cooldown_lookup_failure_restores_the_active_button() -> None:
    bot = _panel_bot()
    original = _profile_link()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.COOLDOWN,
                retry_after_seconds=1200,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(side_effect=RuntimeError("database details")),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    rendered = interaction.edit_original_response.await_args.kwargs
    assert rendered["embed"].fields[-1].value == (
        "Nie udało się sprawdzić możliwości odświeżenia.\nSpróbuj ponownie."
    )
    assert "database" not in rendered["embed"].fields[-1].value
    assert refresh.label == "Odśwież rangę"
    assert refresh.disabled is False
    assert view._refresh_in_progress is False
    interaction.followup.send.assert_not_awaited()


async def test_profile_refresh_keeps_button_disabled_after_observer_error() -> None:
    bot = _panel_bot()
    original = _profile_link()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.ENQUEUED,
                baseline_rank_last_checked_at=original.rank_last_checked_at,
            )
        ),
        get_by_user=AsyncMock(side_effect=RuntimeError("internal database details")),
    )
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, original),
        link=original,
        start_verification=AsyncMock(),
    )
    interaction = SimpleNamespace(
        guild_id=123,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    refresh = next(
        item for item in view.children if item.custom_id == "account-profile:rank-refresh:v1"
    )
    await refresh.callback(interaction)

    kwargs = interaction.edit_original_response.await_args.kwargs
    assert kwargs["content"] is None
    observer_embed = kwargs["embed"]
    assert observer_embed.description is None
    assert observer_embed.fields[-1].name == "Stan odświeżania"
    assert observer_embed.fields[-1].value == (
        "Nie udało się pokazać wyniku odświeżania.\n"
        "Odświeżanie może nadal trwać.\n"
        "Otwórz `/profil` ponownie za chwilę."
    )
    assert "database" not in observer_embed.fields[-1].value
    assert kwargs["view"] is view
    assert refresh.label == "Odświeżanie…"
    assert refresh.disabled is True
    assert view._refresh_in_progress is True


async def test_profile_delete_passes_captured_identity_to_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    link = _profile_link()
    view = AccountProfileView(
        bot,
        owner_id=101,
        presentation=verification._build_account_profile(bot, link),
        link=link,
        start_verification=AsyncMock(),
    )
    show_confirmation = AsyncMock()
    monkeypatch.setattr(verification, "_show_delete_confirmation", show_confirmation)
    interaction = SimpleNamespace()

    delete = next(item for item in view.children if item.custom_id == "account-profile:delete:v1")
    await delete.callback(interaction)

    show_confirmation.assert_awaited_once_with(
        bot,
        interaction,
        expected_puuid=link.puuid,
        expected_platform=link.platform,
        expected_created_at=link.created_at,
    )


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (RankRefreshRequestStatus.ENQUEUED, "czeka w kolejce"),
        (RankRefreshRequestStatus.ALREADY_DUE, "jest już w kolejce"),
        (RankRefreshRequestStatus.ALREADY_CLAIMED, "Trwa już odświeżanie"),
        (RankRefreshRequestStatus.BACKOFF_ACTIVE, "automatycznie"),
        (RankRefreshRequestStatus.LINK_CHANGED, "Dane konta zmieniły się"),
        (RankRefreshRequestStatus.NOT_LINKED, "Najpierw kliknij „Zweryfikuj konto”"),
    ],
)
async def test_refresh_button_returns_private_queue_status(
    monkeypatch: pytest.MonkeyPatch,
    status: RankRefreshRequestStatus,
    fragment: str,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(return_value=RankRefreshRequestResult(status))
    )
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _request_rank_refresh_from_panel(bot, interaction)

    bot.verifications.request_rank_refresh.assert_awaited_once_with(
        123,
        101,
        cooldown_seconds=1800,
        source="user",
    )
    assert fragment in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize(
    ("retry_after_seconds", "expected_message"),
    [
        (1201, "Rangę możesz odświeżyć za około 21 min."),
        (59, "Rangę możesz odświeżyć za 59 s."),
        (0, "Rangę będzie można odświeżyć za chwilę."),
    ],
)
async def test_refresh_button_reports_persistent_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    retry_after_seconds: int,
    expected_message: str,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    bot.verifications = SimpleNamespace(
        request_rank_refresh=AsyncMock(
            return_value=RankRefreshRequestResult(
                RankRefreshRequestStatus.COOLDOWN,
                retry_after_seconds=retry_after_seconds,
            )
        )
    )
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _request_rank_refresh_from_panel(bot, interaction)

    interaction.response.send_message.assert_awaited_once_with(
        expected_message,
        ephemeral=True,
    )


async def test_delete_button_requires_ephemeral_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    link = _profile_link()
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=link))
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _show_delete_confirmation(bot, interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    confirmation = interaction.response.send_message.await_args.args[0]
    assert "Rola „Zweryfikowany” zostanie usunięta." in confirmation
    assert "Role regionu, rangi i użytkownika pozostaną bez zmian." in confirmation
    view = kwargs["view"]
    assert isinstance(view, DeleteVerificationConfirmationView)
    assert view.expected_puuid == link.puuid
    assert view.expected_platform == link.platform
    assert view.expected_created_at == link.created_at
    assert {item.label for item in view.children} == {"Tak, usuń powiązanie", "Anuluj"}


async def test_delete_rejects_a_profile_for_an_old_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    bot = _panel_bot()
    current = _profile_link(puuid="new-puuid")
    bot.verifications = SimpleNamespace(get_by_user=AsyncMock(return_value=current))
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(101),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await _show_delete_confirmation(
        bot,
        interaction,
        expected_puuid="old-puuid",
        expected_platform="EUN1",
        expected_created_at=current.created_at,
    )

    interaction.response.send_message.assert_awaited_once_with(
        "Ten widok jest już nieaktualny. Otwórz `/profil` ponownie.",
        ephemeral=True,
    )


async def test_remove_command_uses_the_same_confirmation_as_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    cog = object.__new__(VerificationCog)
    cog.bot = bot
    show_confirmation = AsyncMock()
    monkeypatch.setattr(verification, "_show_delete_confirmation", show_confirmation)
    interaction = SimpleNamespace()

    await VerificationCog.remove_own_verification.callback(cog, interaction)

    show_confirmation.assert_awaited_once_with(bot, interaction)


async def test_delete_confirmation_calls_the_shared_removal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    remove = AsyncMock(return_value="Usunięto.")
    monkeypatch.setattr(verification, "_remove_user_verification", remove)
    created_at = datetime.now(UTC)
    view = DeleteVerificationConfirmationView(
        bot,
        owner_id=101,
        expected_puuid="puuid-101",
        expected_platform="EUN1",
        expected_created_at=created_at,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101),
        response=SimpleNamespace(edit_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    confirm = next(item for item in view.children if item.custom_id.endswith(":confirm:v1"))

    await confirm.callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(
        content="Usuwanie powiązania…", view=None
    )
    remove.assert_awaited_once_with(
        bot,
        interaction,
        expected_puuid="puuid-101",
        expected_platform="EUN1",
        expected_created_at=created_at,
    )
    interaction.edit_original_response.assert_awaited_once_with(content="Usunięto.", view=None)


async def test_delete_confirmation_hides_unexpected_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _panel_bot()
    monkeypatch.setattr(
        verification,
        "_remove_user_verification",
        AsyncMock(side_effect=RuntimeError("database details")),
    )
    view = DeleteVerificationConfirmationView(
        bot,
        owner_id=101,
        expected_puuid="puuid-101",
        expected_platform="EUN1",
        expected_created_at=datetime.now(UTC),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=101),
        response=SimpleNamespace(edit_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    confirm = next(item for item in view.children if item.custom_id.endswith(":confirm:v1"))

    await confirm.callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(
        content="Usuwanie powiązania…", view=None
    )
    interaction.edit_original_response.assert_awaited_once_with(
        content=("Nie udało się usunąć powiązania. Otwórz `/profil`, aby sprawdzić aktualny stan."),
        view=None,
    )


async def test_shared_delete_keeps_region_rank_and_member_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = object()
    region = object()
    rank = object()
    member_role = object()
    audit_delete = AsyncMock()
    channel = SimpleNamespace(
        get_partial_message=Mock(return_value=SimpleNamespace(delete=audit_delete))
    )
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    member = FakeMember(101, guild=guild)
    member.roles = [verified, region, rank, member_role]
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    monkeypatch.setattr(verification.discord.abc, "Messageable", type(channel))
    monkeypatch.setattr(verification, "find_role", Mock(return_value=verified))
    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        message_id=456,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verified_role_name="Zweryfikowany",
            zweryfikowani_channel_id=789,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=SimpleNamespace(
            request_verification_deletion=AsyncMock(return_value=link),
            finalize_verification_deletion=AsyncMock(return_value=True),
            retry_verification_deletion=AsyncMock(),
        ),
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )
    interaction = SimpleNamespace(guild_id=123, user=member, guild=guild)

    message = await _remove_user_verification(bot, interaction)

    member.remove_roles.assert_awaited_once_with(
        verified, reason="Usunięcie weryfikacji przez użytkownika"
    )
    assert region in member.roles and rank in member.roles and member_role in member.roles
    assert "pozostają bez zmian" in message
    audit_delete.assert_awaited_once()
    bot.verifications.finalize_verification_deletion.assert_awaited_once_with(
        123,
        101,
        expected_puuid="puuid",
        expected_platform="EUN1",
        expected_created_at=created_at,
    )


async def test_shared_delete_reports_pending_and_retries_when_discord_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    guild = SimpleNamespace(get_channel=Mock(return_value=None))
    member = FakeMember(101, guild=guild)
    verified = object()
    member.roles = [verified]
    member.remove_roles = AsyncMock(side_effect=FakeHTTPException())
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification, "find_role", Mock(return_value=verified))
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        message_id=456,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
    )
    repository = SimpleNamespace(
        request_verification_deletion=AsyncMock(return_value=link),
        retry_verification_deletion=AsyncMock(return_value=300),
        finalize_verification_deletion=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verified_role_name="Zweryfikowany",
            zweryfikowani_channel_id=789,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=repository,
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )
    interaction = SimpleNamespace(guild_id=123, user=member, guild=guild)

    message = await _remove_user_verification(bot, interaction)

    assert message == ("Usuwanie powiązania jeszcze trwa. Spróbujemy dokończyć je automatycznie.")
    repository.retry_verification_deletion.assert_awaited_once_with(
        123,
        101,
        expected_created_at=created_at,
        base_delay_seconds=300,
    )
    repository.finalize_verification_deletion.assert_not_awaited()


async def test_shared_delete_retries_audit_cleanup_before_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHTTPException(Exception):
        pass

    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    audit_delete = AsyncMock(side_effect=FakeHTTPException())
    channel = SimpleNamespace(
        get_partial_message=Mock(return_value=SimpleNamespace(delete=audit_delete))
    )
    guild = SimpleNamespace(get_channel=Mock(return_value=channel))
    member = FakeMember(101, guild=guild)
    monkeypatch.setattr(verification.discord, "Member", FakeMember)
    monkeypatch.setattr(verification.discord, "HTTPException", FakeHTTPException)
    monkeypatch.setattr(verification.discord.abc, "Messageable", type(channel))
    monkeypatch.setattr(verification, "find_role", Mock(return_value=None))
    link = SimpleNamespace(
        guild_id=123,
        discord_user_id=101,
        message_id=456,
        puuid="puuid",
        platform="EUN1",
        created_at=created_at,
    )
    repository = SimpleNamespace(
        request_verification_deletion=AsyncMock(return_value=link),
        retry_verification_deletion=AsyncMock(return_value=300),
        finalize_verification_deletion=AsyncMock(),
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            verified_role_name="Zweryfikowany",
            zweryfikowani_channel_id=789,
            rank_refresh_retry_base_seconds=300,
        ),
        verifications=repository,
        verification_sessions=SimpleNamespace(cancel_for_user=AsyncMock()),
    )

    message = await _remove_user_verification(
        bot, SimpleNamespace(guild_id=123, user=member, guild=guild)
    )

    assert message == ("Usuwanie powiązania jeszcze trwa. Spróbujemy dokończyć je automatycznie.")
    repository.retry_verification_deletion.assert_awaited_once()
    repository.finalize_verification_deletion.assert_not_awaited()
