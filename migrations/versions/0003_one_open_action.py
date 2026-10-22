"""At most one open store action per incident.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24

An action is "open" while it waits for the store. Answered (responded) and
closed actions do not count. Existing duplicates are closed first (the oldest
open action of each incident stays open; the others get close_reason 'manual'),
so the unique index can always be created.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLOSE_DUPLICATES = """
UPDATE store_actions
SET status = 'closed', close_reason = 'manual', closed_at = now()
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               row_number() OVER (PARTITION BY incident_id ORDER BY created_at, id) AS position
        FROM store_actions
        WHERE status = 'open'
    ) AS ranked
    WHERE position > 1
)
"""


def upgrade() -> None:
    op.execute(CLOSE_DUPLICATES)
    op.execute(
        "CREATE UNIQUE INDEX ux_action_one_open_per_incident "
        "ON store_actions (incident_id) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_action_one_open_per_incident")
