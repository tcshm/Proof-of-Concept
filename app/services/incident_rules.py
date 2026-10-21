# app/services/incident_rules.py
"""Pure rules for incidents and the journal queue (no database).

can_move / check_transition   which state changes are allowed (no reopening)
journal_process_status        whether the worker will process a journal entry
"""

from __future__ import annotations

from app.core.errors import ConflictError
from app.models.values import ActorType, IncidentState, JournalElement, JournalProcessStatus

STATE_LABELS: dict[int, str] = {
    IncidentState.NEW: "New",
    IncidentState.IN_PROGRESS: "In Progress",
    IncidentState.ON_HOLD: "On Hold",
    IncidentState.RESOLVED: "Resolved",
    IncidentState.CLOSED: "Closed",
    IncidentState.CANCELED: "Canceled",
}

FINAL_STATES: frozenset[int] = frozenset({IncidentState.CLOSED, IncidentState.CANCELED})

# from-state -> states it may move to. Closed and Canceled are final; Resolved
# can only be closed (reopening is out of scope for the POC).
ALLOWED_TRANSITIONS: dict[int, frozenset[int]] = {
    IncidentState.NEW: frozenset(
        {
            IncidentState.IN_PROGRESS,
            IncidentState.ON_HOLD,
            IncidentState.RESOLVED,
            IncidentState.CANCELED,
        }
    ),
    IncidentState.IN_PROGRESS: frozenset(
        {IncidentState.ON_HOLD, IncidentState.RESOLVED, IncidentState.CANCELED}
    ),
    IncidentState.ON_HOLD: frozenset(
        {IncidentState.IN_PROGRESS, IncidentState.RESOLVED, IncidentState.CANCELED}
    ),
    IncidentState.RESOLVED: frozenset({IncidentState.CLOSED}),
    IncidentState.CLOSED: frozenset(),
    IncidentState.CANCELED: frozenset(),
}


def state_label(state: int) -> str:
    return STATE_LABELS.get(state, str(state))


def can_move(current: int, target: int) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def check_transition(number: str, current: int, target: int) -> None:
    if not can_move(current, target):
        raise ConflictError(
            f"Incident {number} is {state_label(current)} and cannot move to {state_label(target)}."
        )


def journal_process_status(element: str, author_type: str) -> JournalProcessStatus:
    """Which journal entries the worker will process (Phase 9).

    pending  the system "Incident created" entry, and every work note or comment
             written by IT staff
    skipped  everything else: store users' replies, and entries written by the
             agent or the system (loop protection)
    """
    if element == JournalElement.SYSTEM:
        return JournalProcessStatus.PENDING
    if author_type == ActorType.IT_STAFF:
        return JournalProcessStatus.PENDING
    return JournalProcessStatus.SKIPPED
