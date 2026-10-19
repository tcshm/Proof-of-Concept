# app/core/clock.py
"""The clock: the only place the backend asks for the current time.

Services and routes never call datetime.now() directly. The API puts one
SystemClock on app.state at startup and hands it to routes through ClockDep;
tests swap in a FixedClock so "today", "overdue" and "expired" are deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that stands still until moved. For tests and repeatable runs."""

    def __init__(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def advance(self, delta: timedelta) -> None:
        self._at = self._at + delta


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("clock times must be timezone-aware")
    return value.astimezone(UTC)
