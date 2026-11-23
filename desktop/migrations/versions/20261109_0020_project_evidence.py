"""Project evidence and durable publication queue; no historical outcomes are changed."""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_0020"
down_revision: str | None = "20261107_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Use explicit columns so the migration remains independent of future models.
    def project() -> sa.Column[str]:
        return sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        )

    def task() -> sa.Column[str]:
        return sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=False,
        )

    def created() -> sa.Column[datetime]:
        return sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )

    op.create_table(
        "project_source_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        project(),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("material", sa.JSON(), nullable=False),
        sa.Column("document_sha256", sa.String(64)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        created(),
        sa.UniqueConstraint("project_id", "identity_hash", "content_hash"),
    )
    op.create_table(
        "project_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        project(),
        task(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("supports", sa.JSON(), nullable=False),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("audit_hash", sa.String(64)),
        sa.Column("indexed_model", sa.String(200)),
        sa.Column("indexed_version", sa.Integer(), nullable=False),
        created(),
        sa.UniqueConstraint("task_id", "ordinal"),
    )
    op.create_table(
        "project_evidence_publications",
        task(),
        project(),
        sa.Column("checkpoint_sequence", sa.Integer(), nullable=False),
        sa.Column("audit_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        created(),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "project_evidence_reuse",
        sa.Column("id", sa.String(36), primary_key=True),
        task(),
        sa.Column(
            "claim_id",
            sa.String(36),
            sa.ForeignKey("project_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        created(),
        sa.UniqueConstraint("task_id", "claim_id"),
    )
    for table, columns in (
        ("project_source_versions", ["project_id"]),
        ("project_claims", ["project_id", "task_id"]),
        ("project_evidence_publications", ["project_id", "status"]),
        ("project_evidence_reuse", ["task_id", "claim_id"]),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table in (
        "project_evidence_reuse",
        "project_evidence_publications",
        "project_claims",
        "project_source_versions",
    ):
        op.drop_table(table)
