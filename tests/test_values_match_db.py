# tests/test_values_match_db.py
"""Every value list in app/models/values.py equals the CHECK constraint in the database."""

from __future__ import annotations

import re
from enum import Enum

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import values as v

# (table, column, enum) for every "column IN (...)" CHECK constraint
VALUE_LISTS: list[tuple[str, str, type[Enum]]] = [
    ("stores", "status", v.StoreStatus),
    ("network_element_types", "business_criticality", v.Criticality),
    ("device_types", "business_criticality", v.Criticality),
    ("devices", "status", v.DeviceStatus),
    ("devices", "connection_type", v.ConnectionType),
    ("network_element_current_status", "status", v.ElementStatus),
    ("raw_network_events", "processing_status", v.RawProcessingStatus),
    ("network_events", "matched_by", v.MatchedBy),
    ("incidents", "state", v.IncidentState),
    ("incidents", "category", v.IncidentCategory),
    ("incidents", "contact_type", v.ContactType),
    ("incidents", "hold_reason", v.HoldReason),
    ("incident_journal", "element", v.JournalElement),
    ("incident_journal", "author_type", v.ActorType),
    ("incident_journal", "source", v.JournalSource),
    ("incident_journal", "process_status", v.JournalProcessStatus),
    ("appointments", "appointment_type", v.AppointmentType),
    ("appointments", "status", v.AppointmentStatus),
    ("appointments", "source", v.AppointmentSource),
    ("ai_prompt_versions", "workflow", v.AiWorkflow),
    ("ai_workflow_runs", "workflow", v.AiWorkflow),
    ("ai_workflow_runs", "status", v.AiRunStatus),
    ("ai_workflow_runs", "outcome", v.AiOutcome),
    ("store_alerts", "severity", v.Severity),
    ("store_alerts", "source", v.AlertSource),
    ("store_alerts", "status", v.AlertStatus),
    ("store_alerts", "close_reason", v.AlertCloseReason),
    ("store_actions", "severity", v.Severity),
    ("store_actions", "source", v.ActionSource),
    ("store_actions", "expected_response", v.ExpectedResponse),
    ("store_actions", "status", v.ActionStatus),
    ("store_actions", "close_reason", v.ActionCloseReason),
    ("store_action_responses", "response_type", v.ExpectedResponse),
    ("store_action_responses", "source", v.ResponseSource),
    ("notifications", "category", v.NotificationCategory),
    ("notifications", "source", v.NotificationSource),
    ("notifications", "system_event", v.SystemEvent),
    ("notifications", "status", v.NotificationStatus),
    ("audit_log", "actor_type", v.ActorType),
    ("audit_log", "source", v.AuditSource),
    ("audit_log", "action", v.AuditAction),
    ("api_request_log", "client_app", v.ClientApp),
    ("event_processing_runs", "mode", v.ProcessingRunMode),
    ("event_processing_runs", "status", v.ProcessingRunStatus),
]

CHECKS_SQL = text(
    """
    SELECT pg_get_constraintdef(c.oid)
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.contype = 'c' AND t.relname = :table
    """
)


def _allowed_values(definitions: list[str], column: str) -> set[str]:
    """Extract the list from PostgreSQL's form: (col = ANY (ARRAY['a'::text, 'b'::text]))."""
    pattern = re.compile(rf'\(\(?"?{column}"?\)? = ANY \(\(?ARRAY\[(.*?)\]')
    for definition in definitions:
        match = pattern.search(definition)
        if match:
            items = [item.strip() for item in match.group(1).split(",")]
            return {re.sub(r"::\w+(\s\w+)?$", "", item).strip("'") for item in items}
    raise AssertionError(f"no 'IN (...)' check found for column {column}")


@pytest.mark.parametrize(
    ("table", "column", "enum"),
    VALUE_LISTS,
    ids=[f"{t}.{c}" for t, c, _ in VALUE_LISTS],
)
async def test_value_list_matches_check(
    db_session: AsyncSession, table: str, column: str, enum: type[Enum]
) -> None:
    definitions = list((await db_session.scalars(CHECKS_SQL, {"table": table})).all())
    assert _allowed_values(definitions, column) == {str(member.value) for member in enum}
