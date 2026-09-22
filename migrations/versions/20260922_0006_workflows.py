"""Add versioned autonomous plans, step runs and review history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0006"
down_revision: str | None = "20260918_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("parent_task_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_agent_tasks_parent",
        "agent_tasks",
        "agent_tasks",
        ["parent_task_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_agent_tasks_parent_task_id", "agent_tasks", ["parent_task_id"])
    for table in ("task_plans", "task_steps", "task_reviews"):
        columns: list[sa.SchemaItem] = [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "task_id",
                sa.String(36),
                sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        ]
        if table == "task_plans":
            columns.extend(
                [
                    sa.Column("version", sa.Integer(), nullable=False),
                    sa.Column("payload", sa.JSON(), nullable=False),
                    sa.Column("reason", sa.Text(), nullable=False),
                    sa.UniqueConstraint("task_id", "version"),
                ]
            )
        elif table == "task_steps":
            columns.extend(
                [
                    sa.Column("plan_version", sa.Integer(), nullable=False),
                    sa.Column("step_id", sa.String(40), nullable=False),
                    sa.Column("child_task_id", sa.String(36), nullable=False, unique=True),
                    sa.Column("status", sa.String(32), nullable=False),
                    sa.Column("result", sa.JSON(), nullable=False),
                ]
            )
        else:
            columns.extend(
                [
                    sa.Column("plan_version", sa.Integer(), nullable=False),
                    sa.Column("payload", sa.JSON(), nullable=False),
                ]
            )
        op.create_table(table, *columns)
        op.create_index(f"ix_{table}_task_id", table, ["task_id"])


def downgrade() -> None:
    for table in ("task_reviews", "task_steps", "task_plans"):
        op.drop_table(table)
    op.drop_index("ix_agent_tasks_parent_task_id", table_name="agent_tasks")
    op.drop_constraint("fk_agent_tasks_parent", "agent_tasks", type_="foreignkey")
    op.drop_column("agent_tasks", "parent_task_id")
