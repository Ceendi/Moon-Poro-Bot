import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from moon_poro.cogs.verification import _report_riot_monitoring
from moon_poro.repositories import RankRefreshQueueStats
from moon_poro.riot import RiotAPIMonitor


async def test_riot_monitoring_logs_api_and_rank_queue_metrics(caplog) -> None:
    now = datetime(2026, 8, 14, 20, 30, tzinfo=UTC)
    last_success = datetime(2026, 8, 14, 20, 20, tzinfo=UTC)
    monitor = RiotAPIMonitor(clock=lambda: last_success)
    for status in (429, 429, 401, 403, 500, 503, 200):
        monitor.record_status(status)
    rank_refresh_queue_stats = AsyncMock(
        return_value=RankRefreshQueueStats(
            due_count=17,
            oldest_due_at=datetime(2026, 8, 14, 20, 25, tzinfo=UTC),
        )
    )
    bot = SimpleNamespace(
        riot_monitor=monitor,
        verifications=SimpleNamespace(rank_refresh_queue_stats=rank_refresh_queue_stats),
        settings=SimpleNamespace(guild_id=123),
    )

    with caplog.at_level(logging.INFO, logger="moon_poro.verification"):
        await _report_riot_monitoring(bot, now=now)

    rank_refresh_queue_stats.assert_awaited_once_with(123)
    message = caplog.messages[-1]
    assert "responses_429_since_start=2" in message
    assert "responses_401_since_start=1" in message
    assert "responses_403_since_start=1" in message
    assert "responses_5xx_since_start=2" in message
    assert "rank_refresh_queue_due=17" in message
    assert "rank_refresh_oldest_due_at_utc=2026-08-14T20:25:00+00:00" in message
    assert "rank_refresh_oldest_overdue_seconds=300" in message
    assert "last_successful_riot_response_utc=2026-08-14T20:20:00+00:00" in message


async def test_riot_monitoring_keeps_api_metrics_when_queue_query_fails(caplog) -> None:
    monitor = RiotAPIMonitor()
    monitor.record_status(503)
    bot = SimpleNamespace(
        riot_monitor=monitor,
        verifications=SimpleNamespace(
            rank_refresh_queue_stats=AsyncMock(side_effect=RuntimeError("database unavailable"))
        ),
        settings=SimpleNamespace(guild_id=123),
    )

    with caplog.at_level(logging.INFO, logger="moon_poro.verification"):
        await _report_riot_monitoring(bot)

    assert "rank_refresh_queue_due=unknown" in caplog.messages[-1]
    assert "responses_5xx_since_start=1" in caplog.messages[-1]
