from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Protocol


class ClaimableOutboxRecord(Protocol):
    claimed_at: datetime | None


class RetryableOutboxRecord(ClaimableOutboxRecord, Protocol):
    failures: int
    next_attempt_at: datetime


def mark_outbox_claimed[RecordT: ClaimableOutboxRecord](
    records: Iterable[RecordT], now: datetime
) -> list[RecordT]:
    claimed = list(records)
    for record in claimed:
        record.claimed_at = now
    return claimed


def reschedule_outbox_record(
    record: RetryableOutboxRecord,
    *,
    now: datetime,
    failures: int,
    delay_seconds: int,
) -> None:
    record.failures = failures
    record.claimed_at = None
    record.next_attempt_at = now + timedelta(seconds=delay_seconds)
