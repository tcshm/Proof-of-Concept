# app/core/status.py
"""Connectivity values and the colours the mobile app shows for them.

    online            -> green
    degraded, unknown -> yellow
    offline           -> red

Every network element and device in an API response carries both:
    status        the colour (what the mock screens use)
    connectivity  the underlying value (for labels and logic)
"""

from __future__ import annotations

from typing import Literal

Colour = Literal["green", "yellow", "red"]

_COLOURS: dict[str, Colour] = {
    "online": "green",
    "degraded": "yellow",
    "unknown": "yellow",
    "offline": "red",
}


def connectivity_or_unknown(value: str | None) -> str:
    """A missing status (no row yet) is reported as unknown."""
    return value if value in _COLOURS else "unknown"


def status_colour(connectivity: str | None) -> Colour:
    return _COLOURS[connectivity_or_unknown(connectivity)]


def tile_colour(total: int, online: int, offline: int) -> Colour:
    """Device tile: green if every device is online, red if every device is
    offline, yellow otherwise (any mix, or any degraded/unknown device)."""
    if total > 0 and online == total:
        return "green"
    if total > 0 and offline == total:
        return "red"
    return "yellow"
