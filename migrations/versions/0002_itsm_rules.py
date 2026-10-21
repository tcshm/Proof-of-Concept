"""ITSM rules: incident number sequence; every appointment belongs to an incident.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20

- incident_number_seq hands out new incident numbers (INC + 8 digits). It starts
  at 2000, above every number used by the seed data, and never gives the same
  number twice, even when two incidents are created at the same moment.
- appointments.incident_id becomes NOT NULL: every appointment, including user
  support visits, is tagged to an incident. (The service also requires the
  incident to be active.)
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS incident_number_seq START WITH 2000 MINVALUE 1")
    op.execute("ALTER TABLE appointments ALTER COLUMN incident_id SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE appointments ALTER COLUMN incident_id DROP NOT NULL")
    op.execute("DROP SEQUENCE IF EXISTS incident_number_seq")
