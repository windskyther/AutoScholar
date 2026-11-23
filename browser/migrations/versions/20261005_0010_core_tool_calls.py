"""Core invocation identities and bounded completion receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261005_0010"
down_revision: str | None = "20261002_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_tool_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_task_id",
            sa.String(36),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("authority_sha256", sa.String(64), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("result_sha256", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    for name in ("task_id", "parent_task_id"):
        op.create_index("ix_core_tool_calls_" + name, "core_tool_calls", [name])


def downgrade() -> None:
    op.drop_table("core_tool_calls")
