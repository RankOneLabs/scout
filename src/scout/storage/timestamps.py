"""Timestamp parsing shared by storage and command adapters."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_aware_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp as an aware UTC instant."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return parsed.astimezone(UTC)
