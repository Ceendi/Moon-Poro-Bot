from moon_poro.rank_refresh import (
    SIX_HOURS,
    TWELVE_HOURS,
    TWENTY_FOUR_HOURS,
    RankSnapshot,
    decide_rank_refresh,
    deterministic_accelerating_jitter,
    effective_refresh_interval,
    is_in_rollout,
    predicted_requests_per_day,
    retry_delay_with_jitter,
    solo_rank_snapshot,
)


def snapshot(
    tier: str = "EMERALD",
    division: str = "II",
    lp: int = 50,
    wins: int = 100,
    losses: int = 90,
) -> RankSnapshot:
    return RankSnapshot(
        tier=tier,
        division=division,
        league_points=lp,
        wins=wins,
        losses=losses,
        inactive=False,
    )


def test_parser_reads_every_league_v4_field_used_by_scheduler() -> None:
    parsed = solo_rank_snapshot(
        [
            {"queueType": "RANKED_FLEX_SR", "tier": "DIAMOND"},
            {
                "queueType": "RANKED_SOLO_5x5",
                "tier": "emerald",
                "rank": "i",
                "leaguePoints": 73,
                "wins": 123,
                "losses": 99,
                "inactive": False,
            },
        ]
    )

    assert parsed == RankSnapshot("EMERALD", "I", 73, 123, 99, False)
    assert solo_rank_snapshot([]) is None


def test_first_snapshot_and_stable_rank_are_24_hours() -> None:
    current = snapshot()

    first = decide_rank_refresh(None, current)
    stable = decide_rank_refresh(current, current)

    assert (first.interval_seconds, first.schedule_class, first.reason) == (
        TWENTY_FOUR_HOURS,
        "24h",
        "first_snapshot",
    )
    assert stable.interval_seconds == TWENTY_FOUR_HOURS


def test_legacy_tier_only_cache_is_treated_as_first_full_snapshot() -> None:
    previous = RankSnapshot(tier="EMERALD")
    decision = decide_rank_refresh(previous, snapshot(tier="DIAMOND"))

    assert decision.interval_seconds == TWENTY_FOUR_HOURS
    assert decision.reason == "first_snapshot"


def test_tier_change_and_active_boundary_are_6_hours() -> None:
    tier_change = decide_rank_refresh(snapshot(tier="PLATINUM"), snapshot())
    active_boundary = decide_rank_refresh(
        snapshot(division="I", lp=49), snapshot(division="I", lp=50, wins=101)
    )

    assert tier_change.interval_seconds == SIX_HOURS
    assert tier_change.tier_changed
    assert active_boundary.interval_seconds == SIX_HOURS
    assert active_boundary.reason == "active_near_boundary"


def test_activity_progress_and_stable_high_tier_are_12_hours() -> None:
    activity = decide_rank_refresh(snapshot(), snapshot(wins=101, lp=55))
    progress = decide_rank_refresh(snapshot(), snapshot(lp=55))
    high_tier = decide_rank_refresh(
        snapshot(tier="MASTER", division="I"), snapshot(tier="MASTER", division="I")
    )

    assert activity.interval_seconds == TWELVE_HOURS
    assert activity.activity_observed
    assert progress.interval_seconds == TWELVE_HOURS
    assert high_tier.interval_seconds == TWELVE_HOURS


def test_inactive_account_is_24_hours_even_in_high_tier() -> None:
    previous = RankSnapshot("MASTER", "I", 100, 100, 90, True)
    decision = decide_rank_refresh(previous, previous)

    assert decision.interval_seconds == TWENTY_FOUR_HOURS
    assert decision.reason == "inactive"


def test_counter_decrease_is_a_reset_not_negative_activity() -> None:
    decision = decide_rank_refresh(snapshot(wins=500, losses=400), snapshot(wins=3, losses=2))

    assert decision.interval_seconds == TWELVE_HOURS
    assert decision.counter_reset
    assert not decision.activity_observed
    assert decision.reason == "counter_reset"


def test_ranked_to_empty_requires_two_successful_empty_responses() -> None:
    previous = snapshot()

    first = decide_rank_refresh(previous, None)
    second = decide_rank_refresh(
        previous,
        None,
        previous_unranked_confirmations=first.unranked_confirmations,
    )

    assert first.snapshot == previous
    assert first.reason == "unranked_confirmation"
    assert first.interval_seconds == 3600
    assert first.unranked_confirmations == 1
    assert second.snapshot.tier == "UNRANKED"
    assert second.reason == "unranked_confirmed"
    assert second.unranked_confirmations == 2


def test_first_empty_snapshot_is_unranked_without_false_transition() -> None:
    decision = decide_rank_refresh(None, None)

    assert decision.snapshot.tier == "UNRANKED"
    assert decision.interval_seconds == TWENTY_FOUR_HOURS
    assert not decision.tier_changed


def test_fixed_shadow_and_rollout_preserve_production_interval() -> None:
    decision = decide_rank_refresh(snapshot(tier="PLATINUM"), snapshot())
    fixed = effective_refresh_interval(
        decision,
        policy="fixed",
        guild_id=1,
        user_id=2,
        rollout_percent=100,
    )
    shadow = effective_refresh_interval(
        decision,
        policy="shadow",
        guild_id=1,
        user_id=2,
        rollout_percent=100,
    )
    outside_rollout = effective_refresh_interval(
        decision,
        policy="adaptive",
        guild_id=1,
        user_id=2,
        rollout_percent=0,
    )

    assert fixed == shadow == outside_rollout == TWENTY_FOUR_HOURS


def test_adaptive_jitter_is_stable_and_only_accelerates_by_at_most_ten_percent() -> None:
    first = deterministic_accelerating_jitter(SIX_HOURS, guild_id=1, user_id=2)
    second = deterministic_accelerating_jitter(SIX_HOURS, guild_id=1, user_id=2)

    assert first == second
    assert SIX_HOURS * 0.9 <= first <= SIX_HOURS


def test_retry_jitter_stays_within_twenty_percent_and_cap() -> None:
    delay = retry_delay_with_jitter(300, 3, guild_id=1, user_id=2)
    capped = retry_delay_with_jitter(300, 16, guild_id=1, user_id=2)

    assert 960 <= delay <= 1440
    assert capped <= 21_600


def test_rollout_assignment_is_deterministic() -> None:
    first = is_in_rollout(guild_id=1, user_id=2, percent=37)
    assert is_in_rollout(guild_id=1, user_id=2, percent=37) is first
    assert not is_in_rollout(guild_id=1, user_id=2, percent=0)
    assert is_in_rollout(guild_id=1, user_id=2, percent=100)


def test_scheduler_capacity_simulation_for_3300_and_10000_accounts() -> None:
    def demand(account_count: int) -> float:
        intervals = (
            [SIX_HOURS] * (account_count * 5 // 100)
            + [TWELVE_HOURS] * (account_count * 25 // 100)
            + [TWENTY_FOUR_HOURS]
            * (account_count - account_count * 5 // 100 - account_count * 25 // 100)
        )
        return predicted_requests_per_day(intervals)

    assert demand(3300) < 86_400 / 10
    assert demand(10_000) < 86_400 / 5
