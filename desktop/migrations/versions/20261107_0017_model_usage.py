"""Record individual model calls without credentials or user content."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261107_0017"
down_revision: str | None = "20261106_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column("api_id", sa.String(64), nullable=False),
        sa.Column("api_name", sa.String(200), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("usage_known", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_model_usage_created", "model_usage", ["created_at"])
    op.create_index("ix_model_usage_api_created", "model_usage", ["api_id", "created_at"])
    op.create_index("ix_model_usage_model_created", "model_usage", ["model", "created_at"])


def downgrade() -> None:
    op.drop_table("model_usage")
