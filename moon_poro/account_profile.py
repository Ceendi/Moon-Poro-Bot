"""Pure presentation helpers for the private Discord account profile."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

import discord

from moon_poro.riot import RANK_TO_ROLE

REFRESH_BUTTON_LABEL: Final = "Odśwież rangę"

_REGION_LABELS: Final = {
    "EUN1": "EUNE",
    "EUW1": "EUW",
    "NA1": "NA",
}


class VerificationLinkLike(Protocol):
    """Read-only subset of ``VerificationLink`` needed by this module."""

    platform: str
    puuid: str | None
    last_known_rank: str | None
    last_known_division: str | None
    last_known_league_points: int | None
    last_known_wins: int | None
    last_known_losses: int | None
    rank_last_checked_at: datetime | None
    rank_refresh_claimed_at: datetime | None
    rank_next_refresh_at: datetime
    rank_refresh_failures: int
    rank_user_refresh_requested_at: datetime | None
    deletion_requested_at: datetime | None


class AccountProfileState(StrEnum):
    """User-facing state selected from cached data and runtime Riot signals."""

    UNVERIFIED = "unverified"
    DELETING = "deleting"
    AUTHORIZATION_UNAVAILABLE = "auth"
    TEMPORARY_UNAVAILABLE = "temporary"
    REFRESH_RUNNING = "running"
    REFRESH_QUEUED = "queued"
    REFRESH_COOLDOWN = "cooldown"
    SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class AccountProfilePresentation:
    """Rendered profile plus metadata used to build its Discord view."""

    state: AccountProfileState
    embed: discord.Embed
    refresh_button_label: str
    refresh_enabled: bool


def build_account_profile(
    link: VerificationLinkLike | None,
    *,
    riot_id: str | None = None,
    now: datetime | None = None,
    manual_refresh_cooldown_seconds: int = 0,
    rank_refresh_claim_timeout_seconds: int = 300,
    riot_temporary_unavailable: bool = False,
    riot_authorization_unavailable: bool = False,
) -> AccountProfilePresentation:
    """Build a Discord profile from cache only, without database or Riot calls.

    Runtime availability flags let the caller include process-local circuit-breaker
    state. The cooldown is calculated from ``rank_user_refresh_requested_at``.
    """

    current_time = _as_utc(now or datetime.now(UTC))
    cooldown_remaining = _cooldown_remaining(
        link,
        now=current_time,
        cooldown_seconds=max(0, manual_refresh_cooldown_seconds),
    )
    state = _classify(
        link,
        now=current_time,
        cooldown_remaining=cooldown_remaining,
        claim_timeout_seconds=max(0, rank_refresh_claim_timeout_seconds),
        riot_temporary_unavailable=riot_temporary_unavailable,
        riot_authorization_unavailable=riot_authorization_unavailable,
    )

    awaiting_first_refresh = (
        state is AccountProfileState.SUCCESS
        and link is not None
        and link.rank_last_checked_at is None
    )
    embed = discord.Embed(
        title="Moje konto",
        description=(
            "Oczekiwanie na pierwszą aktualizację."
            if awaiting_first_refresh
            else _status_text(state, cooldown_remaining)
        ),
        colour=_state_colour(state),
    )
    if state is not AccountProfileState.UNVERIFIED and link is not None:
        _add_profile_fields(embed, link, riot_id=riot_id)

    refresh_enabled = state in {
        AccountProfileState.SUCCESS,
        AccountProfileState.REFRESH_QUEUED,
        AccountProfileState.REFRESH_RUNNING,
        AccountProfileState.TEMPORARY_UNAVAILABLE,
    }
    return AccountProfilePresentation(
        state=state,
        embed=embed,
        refresh_button_label=REFRESH_BUTTON_LABEL,
        refresh_enabled=refresh_enabled,
    )


def _classify(
    link: VerificationLinkLike | None,
    *,
    now: datetime,
    cooldown_remaining: timedelta,
    claim_timeout_seconds: int,
    riot_temporary_unavailable: bool,
    riot_authorization_unavailable: bool,
) -> AccountProfileState:
    if link is None or not link.puuid:
        return AccountProfileState.UNVERIFIED
    if link.deletion_requested_at is not None:
        return AccountProfileState.DELETING
    if riot_authorization_unavailable:
        return AccountProfileState.AUTHORIZATION_UNAVAILABLE
    if link.rank_refresh_claimed_at is not None:
        stale_at = now - timedelta(seconds=claim_timeout_seconds)
        if _as_utc(link.rank_refresh_claimed_at) > stale_at:
            return AccountProfileState.REFRESH_RUNNING
        return AccountProfileState.REFRESH_QUEUED
    if riot_temporary_unavailable:
        return AccountProfileState.TEMPORARY_UNAVAILABLE
    if _as_utc(link.rank_next_refresh_at) <= now:
        return AccountProfileState.REFRESH_QUEUED
    if link.rank_refresh_failures > 0:
        return AccountProfileState.TEMPORARY_UNAVAILABLE
    if cooldown_remaining > timedelta(0):
        return AccountProfileState.REFRESH_COOLDOWN
    return AccountProfileState.SUCCESS


def _cooldown_remaining(
    link: VerificationLinkLike | None,
    *,
    now: datetime,
    cooldown_seconds: int,
) -> timedelta:
    if link is None or link.rank_user_refresh_requested_at is None or cooldown_seconds == 0:
        return timedelta(0)
    available_at = _as_utc(link.rank_user_refresh_requested_at) + timedelta(
        seconds=cooldown_seconds
    )
    return max(available_at - now, timedelta(0))


def _add_profile_fields(
    embed: discord.Embed,
    link: VerificationLinkLike,
    *,
    riot_id: str | None,
) -> None:
    rank = _rank_label(link)
    wins, losses = _valid_record(link.last_known_wins, link.last_known_losses)

    embed.add_field(name="Riot ID", value=_riot_id_label(riot_id), inline=True)
    embed.add_field(name="Region", value=_region_label(link.platform), inline=True)
    embed.add_field(name="Solo/Duo", value=rank, inline=True)
    embed.add_field(name="LP", value=_lp_label(link), inline=True)
    embed.add_field(name="Bilans", value=_record_label(wins, losses), inline=True)
    embed.add_field(name="Win rate", value=_win_rate_label(wins, losses), inline=True)
    embed.add_field(
        name="Ostatnia udana aktualizacja",
        value=_last_update_label(link.rank_last_checked_at),
        inline=False,
    )


def _status_text(state: AccountProfileState, cooldown_remaining: timedelta) -> str:
    if state is AccountProfileState.UNVERIFIED:
        return "Nie masz jeszcze połączonego konta Riot."
    if state is AccountProfileState.DELETING:
        return "Usuwanie powiązania trwa."
    if state is AccountProfileState.AUTHORIZATION_UNAVAILABLE:
        return "Odświeżanie jest teraz niedostępne. Problem wymaga działania administratora."
    if state is AccountProfileState.TEMPORARY_UNAVAILABLE:
        return "Riot jest chwilowo niedostępny. Spróbujemy ponownie automatycznie."
    if state is AccountProfileState.REFRESH_RUNNING:
        return "Odświeżanie rangi trwa."
    if state is AccountProfileState.REFRESH_QUEUED:
        return "Odświeżenie czeka w kolejce."
    if state is AccountProfileState.REFRESH_COOLDOWN:
        minutes = max(1, math.ceil(cooldown_remaining.total_seconds() / 60))
        return f"Kolejne odświeżenie będzie dostępne za około {minutes} min."
    return "Dane z ostatniego sprawdzenia są gotowe."


def _state_colour(state: AccountProfileState) -> discord.Colour:
    if state is AccountProfileState.SUCCESS:
        return discord.Colour.green()
    if state in {AccountProfileState.REFRESH_QUEUED, AccountProfileState.REFRESH_RUNNING}:
        return discord.Colour.blurple()
    if state in {
        AccountProfileState.REFRESH_COOLDOWN,
        AccountProfileState.TEMPORARY_UNAVAILABLE,
    }:
        return discord.Colour.orange()
    if state in {AccountProfileState.DELETING, AccountProfileState.AUTHORIZATION_UNAVAILABLE}:
        return discord.Colour.red()
    return discord.Colour.light_grey()


def _riot_id_label(riot_id: str | None) -> str:
    if riot_id is None or not riot_id.strip():
        return "Niedostępny"
    return discord.utils.escape_markdown(riot_id.strip())


def _region_label(platform: str) -> str:
    normalized = platform.strip().upper()
    if not normalized:
        return "Nieznany"
    return _REGION_LABELS.get(normalized, discord.utils.escape_markdown(normalized))


def _rank_label(link: VerificationLinkLike) -> str:
    if link.last_known_rank is None or not link.last_known_rank.strip():
        return "Brak danych"
    tier = link.last_known_rank.strip().upper()
    if tier == "UNRANKED":
        return "Brak rangi"
    label = RANK_TO_ROLE.get(tier, tier.replace("_", " ").title())
    if tier == "GRANDMASTER":
        label = "Grandmaster"
    division = (link.last_known_division or "").strip().upper()
    return f"{label} {division}".strip()


def _lp_label(link: VerificationLinkLike) -> str:
    if (link.last_known_rank or "").strip().upper() == "UNRANKED":
        return "—"
    if link.last_known_league_points is None or link.last_known_league_points < 0:
        return "—"
    return f"{link.last_known_league_points} LP"


def _valid_record(wins: int | None, losses: int | None) -> tuple[int | None, int | None]:
    if wins is None or losses is None or wins < 0 or losses < 0:
        return None, None
    return wins, losses


def _record_label(wins: int | None, losses: int | None) -> str:
    if wins is None or losses is None:
        return "—"
    return f"{wins} W / {losses} P"


def _win_rate_label(wins: int | None, losses: int | None) -> str:
    if wins is None or losses is None or wins + losses == 0:
        return "—"
    rate = wins / (wins + losses) * 100
    return f"{rate:.1f}".replace(".", ",").removesuffix(",0") + "%"


def _last_update_label(last_checked_at: datetime | None) -> str:
    if last_checked_at is None:
        return "Jeszcze nie sprawdzono"
    timestamp = int(_as_utc(last_checked_at).timestamp())
    return f"<t:{timestamp}:f> • <t:{timestamp}:R>"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "REFRESH_BUTTON_LABEL",
    "AccountProfilePresentation",
    "AccountProfileState",
    "VerificationLinkLike",
    "build_account_profile",
]
