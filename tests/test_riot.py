from moon_poro.riot import get_rank_from_leagues


def test_solo_rank_is_selected() -> None:
    leagues = [
        {"queueType": "RANKED_FLEX_SR", "tier": "DIAMOND"},
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"},
    ]
    assert get_rank_from_leagues(leagues) == "GOLD"


def test_missing_solo_rank_is_unranked() -> None:
    assert get_rank_from_leagues([]) == "UNRANKED"
