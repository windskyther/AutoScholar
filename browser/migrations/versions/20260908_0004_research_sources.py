"""Persist requested research source selection."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_0004"
down_revision: str | None = "20260908_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_tasks",
        sa.Column(
            "research_sources",
            sa.JSON(),
            server_default=sa.text("'[\"web\", \"paper\"]'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_tasks", "research_sources")
