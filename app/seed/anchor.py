# app/seed/anchor.py
"""The seed anchor and relative time offsets.

Every seeded time is written as an offset from the anchor (the seed run time),
so the demo data always looks current: "-5h" means five hours before the anchor.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

WINDOW = timedelta(hours=24)

_OFFSET = re.compile(r"^-(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?$")


def parse_offset(value: str) -> timedelta:
    """'-5h' -> -5 hours, '-2h20m' -> -2 h 20 min, '-90m' -> -90 min."""
    match = _OFFSET.fullmatch(value.strip())
    if match is None or not (match["hours"] or match["minutes"]):
        raise ValueError(f"invalid offset {value!r}: use forms like -5h, -2h20m or -90m")
    return -timedelta(hours=int(match["hours"] or 0), minutes=int(match["minutes"] or 0))


def resolve_anchor(value: str | None = None, now: datetime | None = None) -> datetime:
    """The anchor in UTC, truncated to the minute.

    value: an ISO 8601 time with a timezone, e.g. 2026-10-13T09:00:00Z.
    Without a value the current time is used.
    """
    if value:
        anchor = datetime.fromisoformat(value)
        if anchor.tzinfo is None:
            raise ValueError("--anchor must include a timezone, e.g. 2026-10-13T09:00:00Z")
    else:
        anchor = now or datetime.now(UTC)
    return anchor.astimezone(UTC).replace(second=0, microsecond=0)


def window_start(anchor: datetime) -> datetime:
    return anchor - WINDOW
