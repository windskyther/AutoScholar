"""Preserve complete project/workflow retrieval questions in evidence provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261005_0012"
down_revision: str | None = "20261005_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "evidence", "query", existing_type=sa.String(400), type_=sa.Text(), existing_nullable=False
    )


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM evidence WHERE length(query) > 400"))
    if count:
        raise RuntimeError(
            "Downgrade would truncate evidence queries; preserve or export them first"
        )
    op.alter_column(
        "evidence", "query", existing_type=sa.Text(), type_=sa.String(400), existing_nullable=False
    )
