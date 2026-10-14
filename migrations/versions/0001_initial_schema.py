"""Initial schema: 22 tables in six groups (schema reference v1.2).

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0001_initial_schema.sql"

# Reverse creation order, so no table is dropped while another still references it.
TABLES_IN_DROP_ORDER = (
    "event_processing_runs",
    "api_request_log",
    "audit_log",
    "notification_targets",
    "notifications",
    "store_action_responses",
    "store_actions",
    "store_alerts",
    "ai_workflow_runs",
    "ai_prompt_versions",
    "appointments",
    "incident_journal",
    "incidents",
    "network_element_current_status",
    "network_events",
    "raw_network_events",
    "devices",
    "network_elements",
    "device_types",
    "network_element_types",
    "assignment_groups",
    "stores",
)


def split_sql(script: str) -> list[str]:
    """Split a SQL script into single statements.

    asyncpg runs one statement per call. A statement ends at a line ending with
    ';', except inside a $$ ... $$ function body. Comment-only lines between
    statements are dropped.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_dollar_body = False
    for line in script.splitlines():
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar_body = not in_dollar_body
        if not in_dollar_body and stripped.endswith(";"):
            statements.append("\n".join(buffer).rstrip().removesuffix(";"))
            buffer = []
    if buffer:
        raise ValueError("SQL script ends without ';' after the last statement")
    return statements


def upgrade() -> None:
    for statement in split_sql(SQL_FILE.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES_IN_DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
