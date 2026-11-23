"""Add research evidence and citation fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0002"
down_revision: str | None = "20260906_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tasks",
        sa.Column("mode", sa.String(length=16), server_default="compute", nullable=False),
    )
    op.add_column(
        "agent_tasks",
        sa.Column("citations", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column(
        "agent_tasks",
        sa.Column("warnings", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("citation_key", sa.String(length=16), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("authors", sa.JSON(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("query", sa.String(length=400), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("relevance", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "citation_key"),
    )
    op.create_index("ix_evidence_task_id", "evidence", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_task_id", table_name="evidence")
    op.drop_table("evidence")
    op.drop_column("agent_tasks", "warnings")
    op.drop_column("agent_tasks", "citations")
    op.drop_column("agent_tasks", "mode")
