"""Commit-ordered per-task event cursors; preserve historical event payloads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261006_0013"
down_revision: str | None = "20261005_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tasks",
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("workflow_events", sa.Column("sequence", sa.BigInteger(), nullable=True))
    # Old UUIDs have no causal order for timestamp ties. Assign a stable historical
    # order; new writers use a transactionally locked counter, never timestamps.
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY task_id ORDER BY created_at, id) AS seq
            FROM workflow_events
        )
        UPDATE workflow_events SET sequence =
            (SELECT seq FROM ranked WHERE ranked.id = workflow_events.id)
    """)
    )
    op.execute(
        sa.text("""
        UPDATE agent_tasks SET event_sequence = COALESCE(
            (SELECT MAX(sequence) FROM workflow_events
                WHERE workflow_events.task_id = agent_tasks.id), 0)
    """)
    )
    with op.batch_alter_table("workflow_events") as batch:
        batch.alter_column("sequence", existing_type=sa.BigInteger(), nullable=False)
    op.create_index(
        "ux_workflow_events_task_sequence", "workflow_events", ["task_id", "sequence"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_workflow_events_task_sequence", table_name="workflow_events")
    with op.batch_alter_table("workflow_events") as batch:
        batch.drop_column("sequence")
    with op.batch_alter_table("agent_tasks") as batch:
        batch.drop_column("event_sequence")
