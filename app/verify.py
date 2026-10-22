# app/verify.py
"""Read-only checks that the database follows the rules agreed across phases.

run_checks() returns one CheckResult per rule; scripts/verify.py prints them and
scripts/demo.py runs them after seeding and draining the worker. LLM wording
varies between runs; these rules must always hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.migrate import alembic_config
from app.models import (
    AiWorkflowRun,
    Appointment,
    Incident,
    IncidentJournal,
    NetworkElement,
    NetworkElementCurrentStatus,
    Notification,
    NotificationTarget,
    RawNetworkEvent,
    Store,
    StoreAction,
    StoreAlert,
)
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    ActionSource,
    ActionStatus,
    AlertSource,
    AlertStatus,
    AppointmentStatus,
    JournalElement,
    JournalProcessStatus,
    NotificationSource,
    SystemEvent,
)
from app.worker.guards import answer_of, find_internal_detail, request_problem

EXPECTED_TABLES = 22
ONE_OPEN_ACTION_INDEX = "ux_action_one_open_per_incident"
CHENNAI = "01234"
CHENNAI_INCIDENTS = {
    "INC00001234": "ISP - Primary Link",
    "INC00001235": "POS Terminal 03 - System Offline",
    "INC00001236": "Register 04 - System Offline",
    "INC00001237": "Wireless - Access Point A004",
}
CHENNAI_VISITS = {
    "POS Terminal Hardware Replacement",
    "AT&T Link Restoration",
    "Network Access Point-004 Replacement",
}
CHENNAI_ANNOUNCEMENTS = {
    "Network maintenance activity scheduled tonight at 1 AM - No action needed from stores",
    "Deployment for curbside application planned on 6th Dec - No action needed from stores",
    "Policy Update: New IT Security Guidelines",
    "Store Manager Message: Team Meeting at 3PM",
    "Training Module Available: Network Basics",
    "Software update tonight for mobile devices",
}
MAX_LISTED = 5


@dataclass(frozen=True)
class CheckResult:
    area: str
    name: str
    ok: bool
    detail: str


def _listing(values) -> str:
    values = sorted(str(v) for v in values)
    more = f" (+{len(values) - MAX_LISTED} more)" if len(values) > MAX_LISTED else ""
    return ", ".join(values[:MAX_LISTED]) + more


def _check(area: str, name: str, bad: list, ok_text: str, bad_text: str) -> CheckResult:
    if bad:
        return CheckResult(area, name, False, f"{bad_text}: {_listing(bad)}")
    return CheckResult(area, name, True, ok_text)


async def _structure(session: AsyncSession) -> list[CheckResult]:
    current = await session.scalar(text("SELECT version_num FROM alembic_version"))
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    tables = await session.scalar(
        text(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' AND table_name <> 'alembic_version'"
        )
    )
    index = await session.scalar(
        text(f"SELECT to_regclass('public.{ONE_OPEN_ACTION_INDEX}') IS NOT NULL")
    )
    return [
        CheckResult("structure", "migration at head", current == head,
                    f"revision {current} (head {head})"),
        CheckResult("structure", "22 tables", tables == EXPECTED_TABLES, f"{tables} tables"),
        CheckResult("structure", "one-open-action index", bool(index),
                    "present" if index else f"{ONE_OPEN_ACTION_INDEX} missing"),
    ]  # fmt: skip


async def _network(session: AsyncSession) -> list[CheckResult]:
    pending = await session.scalar(
        select(func.count())
        .select_from(RawNetworkEvent)
        .where(RawNetworkEvent.processing_status == "pending")
    )
    missing = (
        await session.scalars(
            select(NetworkElement.name)
            .outerjoin(
                NetworkElementCurrentStatus,
                NetworkElementCurrentStatus.network_element_id == NetworkElement.id,
            )
            .where(
                NetworkElement.is_active, NetworkElementCurrentStatus.network_element_id.is_(None)
            )
        )
    ).all()
    return [
        CheckResult("network", "raw events processed", pending == 0, f"{pending} pending"),
        _check("network", "status row per element", list(missing), "every active element",
               "no status row"),
    ]  # fmt: skip


async def _itsm(session: AsyncSession) -> list[CheckResult]:
    created = select(IncidentJournal.id).where(
        IncidentJournal.incident_id == Incident.id,
        IncidentJournal.element == JournalElement.SYSTEM,
    )
    no_entry = (await session.scalars(select(Incident.number).where(~created.exists()))).all()
    orphans = (
        await session.scalars(
            select(Incident.number)
            .join(Appointment, Appointment.incident_id == Incident.id)
            .where(
                Appointment.status.in_(
                    (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS)
                ),
                Incident.state.in_((7, 8)),
            )
        )
    ).all()
    return [
        _check("itsm", "created entry per incident", list(no_entry), "every incident",
               "missing for"),
        _check("itsm", "open visits on open incidents", list(orphans), "every open visit",
               "open visits on closed incidents"),
    ]  # fmt: skip


async def _worker(session: AsyncSession) -> list[CheckResult]:
    waiting = dict(
        (
            await session.execute(
                select(IncidentJournal.process_status, func.count())
                .where(
                    IncidentJournal.process_status.in_(
                        (
                            JournalProcessStatus.PENDING,
                            JournalProcessStatus.PROCESSING,
                            JournalProcessStatus.FAILED,
                        )
                    )
                )
                .group_by(IncidentJournal.process_status)
            )
        ).all()
    )
    active_alert = select(StoreAlert.id).where(
        StoreAlert.incident_id == Incident.id, StoreAlert.status == AlertStatus.ACTIVE
    )
    without_alert = (
        await session.scalars(
            select(Incident.number).where(
                Incident.state.in_(ACTIVE_INCIDENT_STATES), ~active_alert.exists()
            )
        )
    ).all()
    stale_alert = (
        await session.scalars(
            select(Incident.number)
            .join(StoreAlert, StoreAlert.incident_id == Incident.id)
            .where(
                StoreAlert.status == AlertStatus.ACTIVE,
                Incident.state.not_in(ACTIVE_INCIDENT_STATES),
            )
        )
    ).all()
    detail = ", ".join(f"{k} {v}" for k, v in sorted(waiting.items())) or "all handled"
    return [
        CheckResult("worker", "journal queue handled", not waiting,
                    detail if not waiting else f"{detail} (run scripts.worker --drain)"),
        _check("worker", "alert per active incident", list(without_alert),
               "every active incident", "no active alert"),
        _check("worker", "no alert on finished incidents", list(stale_alert), "none",
               "active alert on"),
    ]  # fmt: skip


async def _actions(session: AsyncSession, now: datetime) -> list[CheckResult]:
    doubles = (
        await session.scalars(
            select(Incident.number)
            .join(StoreAction, StoreAction.incident_id == Incident.id)
            .where(StoreAction.status == ActionStatus.OPEN)
            .group_by(Incident.number)
            .having(func.count() > 1)
        )
    ).all()
    overdue = (
        await session.scalars(
            select(StoreAction.title).where(
                StoreAction.status == ActionStatus.OPEN,
                StoreAction.due_by.is_not(None),
                StoreAction.due_by <= now,
            )
        )
    ).all()
    notice = select(Notification.id).where(
        Notification.action_id == StoreAction.id,
        Notification.system_event == SystemEvent.ACTION_REQUIRED,
    )
    unnotified = (
        await session.scalars(
            select(StoreAction.title).where(
                StoreAction.source == ActionSource.LLM, ~notice.exists()
            )
        )
    ).all()
    rows = (
        await session.execute(
            select(
                StoreAction.title,
                IncidentJournal.value,
                AiWorkflowRun.input_context,
                AiWorkflowRun.output,
                AiWorkflowRun.status,
            )
            .join(IncidentJournal, IncidentJournal.id == StoreAction.source_journal_id)
            .join(AiWorkflowRun, AiWorkflowRun.id == StoreAction.ai_workflow_run_id)
            .where(StoreAction.source == ActionSource.LLM)
        )
    ).all()
    ungrounded = []
    for title, entry_text, context, output, status in rows:
        task = (context or {}).get("task")
        if task in (None, "confirm_fix") or status == "needs_review":
            continue  # older runs, fix confirmations and fallbacks need no quote
        if request_problem(answer_of(output).get("store_request_quote"), entry_text):
            ungrounded.append(title)
    return [
        _check("actions", "at most one open action per incident", list(doubles),
               "every incident", "several open actions on"),
        _check("actions", "no overdue open action", list(overdue), "none", "overdue"),
        _check("actions", "action has its notification", list(unnotified), "every LLM action",
               "no Action Required notification"),
        _check("actions", "actions grounded in a request", ungrounded, "every LLM action",
               "no quoted store request"),
    ]  # fmt: skip


async def _texts(session: AsyncSession) -> list[CheckResult]:
    found: list[str] = []
    alerts = await session.execute(
        select(StoreAlert.title, StoreAlert.detail).where(StoreAlert.source == AlertSource.LLM)
    )
    actions = await session.execute(
        select(StoreAction.title, StoreAction.detail).where(StoreAction.source == ActionSource.LLM)
    )
    notices = await session.execute(
        select(Notification.title, Notification.description)
        .join(IncidentJournal, IncidentJournal.id == Notification.source_journal_id)
        .where(IncidentJournal.element == JournalElement.WORK_NOTES)
    )
    for result in (alerts, actions, notices):
        for title, body in result.all():
            kind = find_internal_detail(title, body)
            if kind:
                found.append(f'"{title}" ({kind})')
    targets = (
        select(func.count())
        .select_from(NotificationTarget)
        .where(NotificationTarget.notification_id == Notification.id)
        .scalar_subquery()
    )
    wrong_targets = (
        await session.scalars(
            select(Notification.title).where(
                Notification.source == NotificationSource.SYSTEM, targets != 1
            )
        )
    ).all()
    return [
        _check("text", "no internal details shown to stores", found, "none found", "found in"),
        _check("notifications", "system notice has one store", list(wrong_targets), "all",
               "wrong number of stores"),
    ]  # fmt: skip


async def _chennai(session: AsyncSession) -> list[CheckResult]:
    store_id = await session.scalar(select(Store.id).where(Store.site_code == CHENNAI))
    if store_id is None:
        return [CheckResult("chennai", "mock store 01234", False, "store 01234 not found")]
    incidents = dict(
        (
            await session.execute(
                select(Incident.number, Incident.short_description).where(
                    Incident.store_id == store_id, Incident.state.in_(ACTIVE_INCIDENT_STATES)
                )
            )
        ).all()
    )
    wrong = [n for n, t in CHENNAI_INCIDENTS.items() if incidents.get(n) != t]
    visits = set(
        (
            await session.scalars(
                select(Appointment.purpose).where(Appointment.store_id == store_id)
            )
        ).all()
    )
    titles = set(
        (
            await session.scalars(
                select(Notification.title)
                .join(NotificationTarget, NotificationTarget.notification_id == Notification.id)
                .where(NotificationTarget.store_id == store_id)
            )
        ).all()
    )
    return [
        _check("chennai", "mock incidents active", wrong, "INC00001234-INC00001237",
               "missing or changed"),
        _check("chennai", "mock appointments", sorted(CHENNAI_VISITS - visits), "apt-001 to 003",
               "missing"),
        _check("chennai", "mock announcements", sorted(CHENNAI_ANNOUNCEMENTS - titles),
               "n-001, n-002, n-008 to n-011", "missing"),
    ]  # fmt: skip


async def run_checks(session: AsyncSession, *, now: datetime) -> list[CheckResult]:
    results: list[CheckResult] = []
    results += await _structure(session)
    results += await _network(session)
    results += await _itsm(session)
    results += await _worker(session)
    results += await _actions(session, now)
    results += await _texts(session)
    results += await _chennai(session)
    return results
