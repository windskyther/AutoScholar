"""Durable asynchronous MCP experiment execution identities."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261005_0011"
down_revision: str | None = "20261005_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_experiment_executions",
        sa.Column(
            "id",
            sa.String(36),
            sa.ForeignKey("tool_operations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("owner", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("heartbeat", sa.Float(), nullable=False),
        sa.Column("deadline", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_experiment_executions")
