from unittest.mock import AsyncMock

import pytest

from moon_poro.riot import (
    RiotAPIUnavailable,
    get_rank_from_leagues,
    profile_icon_url,
    riot_api_call,
)


class RiotResponseError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def test_solo_rank_is_selected() -> None:
    leagues = [
        {"queueType": "RANKED_FLEX_SR", "tier": "DIAMOND"},
        {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"},
    ]
    assert get_rank_from_leagues(leagues) == "GOLD"


def test_missing_solo_rank_is_unranked() -> None:
    assert get_rank_from_leagues([]) == "UNRANKED"


def test_missing_tier_is_unranked() -> None:
    assert get_rank_from_leagues([{"queueType": "RANKED_SOLO_5x5"}]) == "UNRANKED"


def test_profile_icon_url_uses_requested_icon() -> None:
    assert profile_icon_url(17).endswith("/17.jpg")


async def test_riot_api_call_returns_successful_response() -> None:
    operation = AsyncMock(return_value={"puuid": "value"})

    result = await riot_api_call(operation)

    assert result == {"puuid": "value"}
    operation.assert_awaited_once_with()


async def test_riot_api_call_maps_not_found_to_fallback() -> None:
    operation = AsyncMock(side_effect=RiotResponseError(404))

    result: list[object] | None = await riot_api_call(operation, not_found=[])

    assert result == []
    operation.assert_awaited_once_with()


@pytest.mark.parametrize("status", [429, 503])
async def test_riot_api_call_does_not_repeat_client_retries(status: int) -> None:
    operation = AsyncMock(side_effect=RiotResponseError(status))

    with pytest.raises(RiotAPIUnavailable) as raised:
        await riot_api_call(operation)

    assert isinstance(raised.value.__cause__, RiotResponseError)
    operation.assert_awaited_once_with()


async def test_riot_api_call_does_not_retry_client_error() -> None:
    operation = AsyncMock(side_effect=RiotResponseError(401))

    with pytest.raises(RiotAPIUnavailable):
        await riot_api_call(operation)

    operation.assert_awaited_once_with()
