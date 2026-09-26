"""Durable workflow queue, checkpoints, approvals and project-scoped memory."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0008"
down_revision: str | None = "20260922_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def _task(*, primary: bool = False) -> sa.Column[sa.String]:
    return sa.Column(
        "task_id",
        sa.String(36),
        sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
        primary_key=primary,
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "workflow_jobs",
        _task(primary=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checkpoint_sequence", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(36)),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("active", sa.JSON(), nullable=False),
        sa.Column("pending_calls", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("active_seconds", sa.Float(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        _created(),
    )
    op.create_index("ix_workflow_jobs_status", "workflow_jobs", ["status"])
    op.create_table(
        "workflow_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        _task(),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        _created(),
        sa.UniqueConstraint("task_id", "sequence"),
    )
    op.create_table(
        "workflow_events",
        sa.Column("id", sa.String(36), primary_key=True),
        _task(),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        _created(),
    )
    op.create_table(
        "workflow_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        _task(),
        sa.Column("operation_sha256", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        _created(),
        sa.UniqueConstraint("task_id", "operation_sha256"),
    )
    op.create_table(
        "project_memories",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "experience_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _task(),
        sa.Column("problem_code", sa.String(100), nullable=False),
        sa.Column("step_id", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _created(),
        sa.UniqueConstraint("task_id", "problem_code", "step_id"),
    )
    for table in (
        "workflow_checkpoints",
        "workflow_events",
        "workflow_approvals",
        "experience_memories",
    ):
        op.create_index(f"ix_{table}_task_id", table, ["task_id"])
    op.create_index("ix_experience_memories_project_id", "experience_memories", ["project_id"])


def downgrade() -> None:
    for table in (
        "experience_memories",
        "project_memories",
        "workflow_approvals",
        "workflow_events",
        "workflow_checkpoints",
        "workflow_jobs",
    ):
        op.drop_table(table)
