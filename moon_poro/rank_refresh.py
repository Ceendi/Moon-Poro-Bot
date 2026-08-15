from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

type RankRefreshPolicy = Literal["fixed", "shadow", "adaptive"]

SIX_HOURS = 6 * 60 * 60
TWELVE_HOURS = 12 * 60 * 60
TWENTY_FOUR_HOURS = 24 * 60 * 60
UNRANKED_CONFIRMATION_SECONDS = 60 * 60
HIGH_TIERS = frozenset({"MASTER", "GRANDMASTER", "CHALLENGER"})


@dataclass(frozen=True, slots=True)
class RankSnapshot:
    tier: str
    division: str | None = None
    league_points: int | None = None
    wins: int | None = None
    losses: int | None = None
    inactive: bool | None = None

    @property
    def games(self) -> int | None:
        if self.wins is None or self.losses is None:
            return None
        return self.wins + self.losses


@dataclass(frozen=True, slots=True)
class RankRefreshDecision:
    snapshot: RankSnapshot
    interval_seconds: int
    schedule_class: str
    reason: str
    activity_observed: bool
    tier_changed: bool
    counter_reset: bool
    unranked_confirmations: int


def solo_rank_snapshot(leagues: list[dict[str, Any]]) -> RankSnapshot | None:
    for league in leagues:
        if league.get("queueType") != "RANKED_SOLO_5x5":
            continue
        return RankSnapshot(
            tier=str(league.get("tier") or "UNRANKED").upper(),
            division=_optional_upper_string(league.get("rank")),
            league_points=_optional_int(league.get("leaguePoints")),
            wins=_optional_int(league.get("wins")),
            losses=_optional_int(league.get("losses")),
            inactive=_optional_bool(league.get("inactive")),
        )
    return None


def decide_rank_refresh(
    previous: RankSnapshot | None,
    current: RankSnapshot | None,
    *,
    previous_unranked_confirmations: int = 0,
) -> RankRefreshDecision:
    """Choose the next League-v4 refresh without performing any I/O.

    A missing ``current`` means a successful HTTP 200 with no Solo/Duo entry. HTTP
    errors, including 404, must be handled before calling this function.
    """

    if current is None:
        return _decide_empty(previous, previous_unranked_confirmations)

    first_snapshot = previous is None or (
        previous.tier != "UNRANKED"
        and previous.division is None
        and previous.league_points is None
        and previous.games is None
    )
    tier_changed = previous is not None and previous.tier != current.tier
    previous_games = previous.games if previous is not None else None
    current_games = current.games
    activity_observed = (
        previous_games is not None and current_games is not None and current_games > previous_games
    )
    counter_reset = (
        previous_games is not None and current_games is not None and current_games < previous_games
    )
    division_or_lp_changed = previous is not None and (
        previous.division != current.division or previous.league_points != current.league_points
    )

    if first_snapshot:
        interval, schedule_class, reason = TWENTY_FOUR_HOURS, "24h", "first_snapshot"
    elif tier_changed:
        interval, schedule_class, reason = SIX_HOURS, "6h", "tier_changed"
    elif current.inactive:
        interval, schedule_class, reason = TWENTY_FOUR_HOURS, "24h", "inactive"
    elif current.tier in HIGH_TIERS and activity_observed:
        interval, schedule_class, reason = SIX_HOURS, "6h", "active_high_tier"
    elif activity_observed and _near_tier_boundary(current):
        interval, schedule_class, reason = SIX_HOURS, "6h", "active_near_boundary"
    elif activity_observed:
        interval, schedule_class, reason = TWELVE_HOURS, "12h", "activity_observed"
    elif division_or_lp_changed:
        interval, schedule_class, reason = TWELVE_HOURS, "12h", "rank_progress_changed"
    elif counter_reset:
        interval, schedule_class, reason = TWELVE_HOURS, "12h", "counter_reset"
    elif current.tier in HIGH_TIERS:
        interval, schedule_class, reason = TWELVE_HOURS, "12h", "stable_high_tier"
    else:
        interval, schedule_class, reason = TWENTY_FOUR_HOURS, "24h", "stable"

    return RankRefreshDecision(
        snapshot=current,
        interval_seconds=interval,
        schedule_class=schedule_class,
        reason=reason,
        activity_observed=activity_observed,
        tier_changed=tier_changed,
        counter_reset=counter_reset,
        unranked_confirmations=0,
    )


def effective_refresh_interval(
    decision: RankRefreshDecision,
    *,
    policy: RankRefreshPolicy,
    guild_id: int,
    user_id: int,
    rollout_percent: int,
    fixed_interval_seconds: int = TWENTY_FOUR_HOURS,
) -> int:
    if decision.reason == "unranked_confirmation":
        return deterministic_accelerating_jitter(
            decision.interval_seconds, guild_id=guild_id, user_id=user_id
        )
    if policy != "adaptive" or not is_in_rollout(
        guild_id=guild_id, user_id=user_id, percent=rollout_percent
    ):
        return fixed_interval_seconds
    return deterministic_accelerating_jitter(
        decision.interval_seconds, guild_id=guild_id, user_id=user_id
    )


def deterministic_accelerating_jitter(interval_seconds: int, *, guild_id: int, user_id: int) -> int:
    """Apply stable jitter in [0%, 10%], only making a deadline earlier."""

    bucket = _stable_bucket("schedule", guild_id, user_id, modulo=10_001)
    reduction = interval_seconds * bucket // 100_000
    return max(1, interval_seconds - reduction)


def retry_delay_with_jitter(
    base_delay_seconds: int,
    failures: int,
    *,
    guild_id: int,
    user_id: int,
    max_delay_seconds: int = 21_600,
) -> int:
    """Return capped exponential backoff with deterministic jitter in [-20%, +20%]."""

    uncapped = base_delay_seconds * (1 << max(0, failures - 1))
    base = min(uncapped, max_delay_seconds)
    bucket = _stable_bucket("retry", guild_id, user_id, failures, modulo=40_001)
    basis_points = bucket - 20_000
    jittered = base + (base * basis_points // 100_000)
    return max(1, min(jittered, max_delay_seconds))


def is_in_rollout(*, guild_id: int, user_id: int, percent: int) -> bool:
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return _stable_bucket("rollout", guild_id, user_id, modulo=100) < percent


def predicted_requests_per_day(intervals_seconds: list[int]) -> float:
    return sum(TWENTY_FOUR_HOURS / interval for interval in intervals_seconds if interval > 0)


def _decide_empty(
    previous: RankSnapshot | None,
    previous_unranked_confirmations: int,
) -> RankRefreshDecision:
    if previous is not None and previous.tier != "UNRANKED" and previous_unranked_confirmations < 1:
        return RankRefreshDecision(
            snapshot=previous,
            interval_seconds=UNRANKED_CONFIRMATION_SECONDS,
            schedule_class="confirm",
            reason="unranked_confirmation",
            activity_observed=False,
            tier_changed=False,
            counter_reset=False,
            unranked_confirmations=1,
        )

    snapshot = RankSnapshot(tier="UNRANKED")
    tier_changed = previous is not None and previous.tier != "UNRANKED"
    return RankRefreshDecision(
        snapshot=snapshot,
        interval_seconds=(SIX_HOURS if tier_changed else TWENTY_FOUR_HOURS),
        schedule_class="6h" if tier_changed else "24h",
        reason="unranked_confirmed" if tier_changed else "stable_unranked",
        activity_observed=False,
        tier_changed=tier_changed,
        counter_reset=False,
        unranked_confirmations=2 if tier_changed else 0,
    )


def _near_tier_boundary(snapshot: RankSnapshot) -> bool:
    if snapshot.division == "I":
        return snapshot.league_points is not None and snapshot.league_points >= 50
    if snapshot.division == "IV":
        return snapshot.league_points is not None and snapshot.league_points <= 25
    return False


def _stable_bucket(*parts: object, modulo: int) -> int:
    payload = ":".join(str(part) for part in parts).encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") % modulo


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_upper_string(value: object) -> str | None:
    return str(value).upper() if value is not None else None
