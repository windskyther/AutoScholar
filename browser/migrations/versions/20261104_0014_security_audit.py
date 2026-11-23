"""Append-only request security audit, without business payloads or credentials."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261104_0014"
down_revision: str | None = "20261006_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column("project_id", sa.String(128), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_security_audit_request_id", "security_audit", ["request_id"])
    op.create_index("ix_security_audit_task_id", "security_audit", ["task_id"])


def downgrade() -> None:
    op.drop_table("security_audit")
