"""Store user-selected task references and continuation links."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261122_0022"
down_revision: str | None = "20261109_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_links",
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "source_task_id",
            sa.String(36),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_index("ix_task_links_source_task_id", "task_links", ["source_task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_links_source_task_id", table_name="task_links")
    op.drop_table("task_links")
