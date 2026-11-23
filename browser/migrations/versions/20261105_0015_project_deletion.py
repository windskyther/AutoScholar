"""Retain project histories while removing deleted projects from the workbench."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261105_0015"
down_revision: str | None = "20261104_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "deleted_at")
