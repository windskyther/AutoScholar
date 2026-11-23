"""Persist named model connections separately from workflow checkpoints."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261106_0016"
down_revision: str | None = "20261105_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("compatibility_profile", sa.String(16), nullable=False),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("model_profiles")
