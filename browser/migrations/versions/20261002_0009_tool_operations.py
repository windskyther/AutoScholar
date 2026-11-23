"""Service-side MCP operation receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0009"
down_revision: str | None = "20260926_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("arguments_sha256", sa.String(64), nullable=False),
        sa.Column("authority_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_tool_operations_task_id", "tool_operations", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_operations_task_id", table_name="tool_operations")
    op.drop_table("tool_operations")
