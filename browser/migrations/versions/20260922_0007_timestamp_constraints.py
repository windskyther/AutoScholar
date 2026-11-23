"""Align legacy timestamp nullability with the existing ORM contract.

No timestamps are backfilled or changed. Unexpected NULLs fail the transactional
migration instead of inventing historical dates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0007"
down_revision: str | None = "20260922_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPS = {
    "agent_tasks": ("created_at", "updated_at"),
    "artifacts": ("created_at",),
    "document_chunks": ("created_at",),
    "document_jobs": ("available_at", "created_at", "updated_at"),
    "documents": ("created_at", "updated_at"),
    "evidence": ("created_at",),
    "experiments": ("created_at", "updated_at"),
    "projects": ("created_at", "updated_at"),
    "tool_calls": ("created_at",),
}


def upgrade() -> None:
    for table, columns in TIMESTAMPS.items():
        for column in columns:
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )


def downgrade() -> None:
    for table, columns in TIMESTAMPS.items():
        for column in columns:
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                nullable=True,
            )
