from __future__ import annotations

from datetime import UTC, datetime


def timestamps_match(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    normalized_left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    normalized_right = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return normalized_left == normalized_right
