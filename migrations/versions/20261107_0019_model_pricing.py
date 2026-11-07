"""Optional immutable rates and per-call estimates, without historical repricing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261107_0019"
down_revision: str | None = "20261107_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("profile_id", sa.String(36), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    for column in (
        sa.Column("pricing_snapshot", sa.JSON(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(24, 10), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
    ):
        op.add_column("model_usage", column)


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        for name in ("currency", "estimated_cost", "pricing_snapshot"):
            batch.drop_column(name)
    op.drop_table("model_prices")
