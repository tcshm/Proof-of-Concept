# app/core/actor.py
"""ActorContext: who is making a change.

Built by route dependencies (mobile and portal users) or by callers without an
HTTP request (worker, seed, scripts), and passed to every service that writes
data. Services never read HTTP headers themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.values import ActorType


@dataclass(frozen=True)
class ActorContext:
    actor_type: str
    actor: str
    source: str
    request_id: uuid.UUID | None = None

    @classmethod
    def system(cls, actor: str, source: str) -> ActorContext:
        """For callers without an HTTP request: the worker, the seed, scripts."""
        return cls(actor_type=ActorType.SYSTEM, actor=actor, source=source)
