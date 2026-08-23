from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from moon_poro.account_profile import (
    REFRESH_BUTTON_LABEL,
    AccountProfileState,
    build_account_profile,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _link(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "platform": "EUN1",
        "puuid": "puuid-1",
        "last_known_rank": "EMERALD",
        "last_known_division": "IV",
        "last_known_league_points": 20,
        "last_known_wins": 120,
        "last_known_losses": 100,
        "rank_last_checked_at": NOW - timedelta(hours=2),
        "rank_refresh_claimed_at": None,
        "rank_next_refresh_at": NOW + timedelta(hours=6),
        "rank_refresh_failures": 0,
        "rank_user_refresh_requested_at": None,
        "deletion_requested_at": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("link", "kwargs", "expected"),
    [
        (None, {}, AccountProfileState.UNVERIFIED),
        (_link(puuid=None), {}, AccountProfileState.UNVERIFIED),
        (
            _link(deletion_requested_at=NOW),
            {},
            AccountProfileState.DELETING,
        ),
        (
            _link(),
            {"riot_authorization_unavailable": True},
            AccountProfileState.AUTHORIZATION_UNAVAILABLE,
        ),
        (
            _link(rank_refresh_claimed_at=NOW),
            {"riot_temporary_unavailable": True},
            AccountProfileState.REFRESH_RUNNING,
        ),
        (
            _link(),
            {"riot_temporary_unavailable": True},
            AccountProfileState.TEMPORARY_UNAVAILABLE,
        ),
        (
            _link(rank_next_refresh_at=NOW),
            {},
            AccountProfileState.REFRESH_QUEUED,
        ),
        (
            _link(rank_refresh_failures=2),
            {},
            AccountProfileState.TEMPORARY_UNAVAILABLE,
        ),
        (
            _link(rank_user_refresh_requested_at=NOW - timedelta(minutes=20)),
            {"manual_refresh_cooldown_seconds": 30 * 60},
            AccountProfileState.REFRESH_COOLDOWN,
        ),
        (_link(), {}, AccountProfileState.SUCCESS),
    ],
)
def test_build_account_profile_classifies_states(
    link: SimpleNamespace | None,
    kwargs: dict[str, Any],
    expected: AccountProfileState,
) -> None:
    profile = build_account_profile(link, now=NOW, **kwargs)

    assert profile.state is expected


def test_running_and_queued_take_priority_over_cooldown() -> None:
    recently_requested = NOW - timedelta(minutes=5)
    running = build_account_profile(
        _link(
            rank_refresh_claimed_at=NOW,
            rank_user_refresh_requested_at=recently_requested,
        ),
        now=NOW,
        manual_refresh_cooldown_seconds=30 * 60,
    )
    queued = build_account_profile(
        _link(
            rank_next_refresh_at=NOW,
            rank_user_refresh_requested_at=recently_requested,
        ),
        now=NOW,
        manual_refresh_cooldown_seconds=30 * 60,
    )

    assert running.state is AccountProfileState.REFRESH_RUNNING
    assert queued.state is AccountProfileState.REFRESH_QUEUED


def test_refresh_claim_timeout_boundary_matches_queue_recovery() -> None:
    still_running = build_account_profile(
        _link(rank_refresh_claimed_at=NOW - timedelta(seconds=299)),
        now=NOW,
        rank_refresh_claim_timeout_seconds=300,
    )
    recoverable = build_account_profile(
        _link(rank_refresh_claimed_at=NOW - timedelta(seconds=300)),
        now=NOW,
    )

    assert still_running.state is AccountProfileState.REFRESH_RUNNING
    assert recoverable.state is AccountProfileState.REFRESH_QUEUED


def test_authorization_message_does_not_claim_an_alert_was_sent() -> None:
    profile = build_account_profile(
        _link(),
        now=NOW,
        riot_authorization_unavailable=True,
    )

    assert profile.embed.description == (
        "Odświeżanie jest teraz niedostępne. Problem wymaga działania administratora."
    )


def test_rendered_profile_uses_cached_rank_data() -> None:
    profile = build_account_profile(_link(), riot_id="Moon Poro#EUNE", now=NOW)
    fields = {field.name: field.value for field in profile.embed.fields}
    expected_timestamp = int((NOW - timedelta(hours=2)).timestamp())

    assert profile.state is AccountProfileState.SUCCESS
    assert profile.embed.title == "Moje konto"
    assert fields == {
        "Riot ID": "Moon Poro#EUNE",
        "Region": "EUNE",
        "Solo/Duo": "Emerald IV",
        "LP": "20 LP",
        "Bilans": "120 W / 100 P",
        "Win rate": "54,5%",
        "Ostatnia udana aktualizacja": (f"<t:{expected_timestamp}:f> • <t:{expected_timestamp}:R>"),
    }


def test_unranked_profile_does_not_invent_lp_or_results() -> None:
    profile = build_account_profile(
        _link(
            last_known_rank="UNRANKED",
            last_known_division=None,
            last_known_league_points=None,
            last_known_wins=None,
            last_known_losses=None,
        ),
        riot_id="Poro#EUW",
        now=NOW,
    )
    fields = {field.name: field.value for field in profile.embed.fields}

    assert fields["Solo/Duo"] == "Brak rangi"
    assert fields["LP"] == "—"
    assert fields["Bilans"] == "—"
    assert fields["Win rate"] == "—"


def test_grandmaster_profile_keeps_user_facing_capitalization() -> None:
    profile = build_account_profile(
        _link(last_known_rank="GRANDMASTER", last_known_division=None),
        riot_id="Poro#EUNE",
        now=NOW,
    )
    fields = {field.name: field.value for field in profile.embed.fields}

    assert fields["Solo/Duo"] == "Grandmaster"


def test_missing_cache_is_described_without_a_riot_call() -> None:
    profile = build_account_profile(
        _link(
            platform="",
            last_known_rank=None,
            last_known_division=None,
            last_known_league_points=None,
            last_known_wins=None,
            last_known_losses=None,
            rank_last_checked_at=None,
        ),
        riot_id=None,
        now=NOW,
    )
    fields = {field.name: field.value for field in profile.embed.fields}

    assert fields["Riot ID"] == "Niedostępny"
    assert fields["Region"] == "Nieznany"
    assert fields["Solo/Duo"] == "Brak danych"
    assert fields["Ostatnia udana aktualizacja"] == "Jeszcze nie sprawdzono"
    assert profile.embed.description == "Oczekiwanie na pierwszą aktualizację."


def test_cooldown_text_rounds_remaining_time_up() -> None:
    profile = build_account_profile(
        _link(rank_user_refresh_requested_at=NOW - timedelta(seconds=1199)),
        now=NOW,
        manual_refresh_cooldown_seconds=30 * 60,
    )

    assert profile.state is AccountProfileState.REFRESH_COOLDOWN
    assert profile.embed.description == ("Kolejne odświeżenie będzie dostępne za około 11 min.")


@pytest.mark.parametrize(
    ("state", "expected_enabled"),
    [
        (AccountProfileState.UNVERIFIED, False),
        (AccountProfileState.DELETING, False),
        (AccountProfileState.AUTHORIZATION_UNAVAILABLE, False),
        (AccountProfileState.REFRESH_COOLDOWN, False),
        (AccountProfileState.TEMPORARY_UNAVAILABLE, True),
        (AccountProfileState.REFRESH_RUNNING, True),
        (AccountProfileState.REFRESH_QUEUED, True),
        (AccountProfileState.SUCCESS, True),
    ],
)
def test_refresh_button_availability(
    state: AccountProfileState,
    expected_enabled: bool,
) -> None:
    link: SimpleNamespace | None = _link()
    kwargs: dict[str, Any] = {}
    if state is AccountProfileState.UNVERIFIED:
        link = None
    elif state is AccountProfileState.DELETING:
        link = _link(deletion_requested_at=NOW)
    elif state is AccountProfileState.AUTHORIZATION_UNAVAILABLE:
        kwargs["riot_authorization_unavailable"] = True
    elif state is AccountProfileState.REFRESH_COOLDOWN:
        link = _link(rank_user_refresh_requested_at=NOW)
        kwargs["manual_refresh_cooldown_seconds"] = 30 * 60
    elif state is AccountProfileState.TEMPORARY_UNAVAILABLE:
        kwargs["riot_temporary_unavailable"] = True
    elif state is AccountProfileState.REFRESH_RUNNING:
        link = _link(rank_refresh_claimed_at=NOW)
    elif state is AccountProfileState.REFRESH_QUEUED:
        link = _link(rank_next_refresh_at=NOW)

    profile = build_account_profile(link, now=NOW, **kwargs)

    assert profile.state is state
    assert profile.refresh_enabled is expected_enabled
    assert profile.refresh_button_label == REFRESH_BUTTON_LABEL == "Odśwież rangę"


def test_presentation_never_offers_check_status_or_technical_errors() -> None:
    profiles = [
        build_account_profile(_link(), now=NOW, riot_temporary_unavailable=True),
        build_account_profile(_link(), now=NOW, riot_authorization_unavailable=True),
    ]

    for profile in profiles:
        rendered = str(profile.embed.to_dict())
        assert "Sprawdź stan" not in rendered
        assert "401" not in rendered
        assert "403" not in rendered
        assert "token" not in rendered.lower()
        assert "exception" not in rendered.lower()


def test_naive_database_datetimes_are_treated_as_utc() -> None:
    naive_checked_at = datetime(2026, 8, 23, 10, 0)
    profile = build_account_profile(
        _link(rank_last_checked_at=naive_checked_at),
        now=NOW,
    )
    fields = {field.name: field.value for field in profile.embed.fields}

    assert fields["Ostatnia udana aktualizacja"] == (
        f"<t:{int(naive_checked_at.replace(tzinfo=UTC).timestamp())}:f> • "
        f"<t:{int(naive_checked_at.replace(tzinfo=UTC).timestamp())}:R>"
    )
