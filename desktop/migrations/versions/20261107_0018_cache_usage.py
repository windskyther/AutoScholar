"""Preserve unknown historical cache usage and label new model call stages."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261107_0018"
down_revision: str | None = "20261107_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("cached_input_tokens", "uncached_input_tokens", "reasoning_tokens"):
        op.add_column("model_usage", sa.Column(name, sa.BigInteger(), nullable=True))
    op.add_column(
        "model_usage", sa.Column("stage", sa.String(24), nullable=False, server_default="other")
    )


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        for name in ("stage", "reasoning_tokens", "uncached_input_tokens", "cached_input_tokens"):
            batch.drop_column(name)
