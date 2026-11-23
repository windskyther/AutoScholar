"""Project-specific source recheck intervals, with no implicit backfill."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261109_0021"
down_revision: str | None = "20261109_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_claims",
        sa.Column("source_types", sa.String(40), nullable=False, server_default=""),
    )
    claims = sa.table(
        "project_claims",
        sa.column("id", sa.String),
        sa.column("supports", sa.JSON),
        sa.column("source_types", sa.String),
    )
    sources = sa.table(
        "project_source_versions", sa.column("id", sa.String), sa.column("material", sa.JSON)
    )
    connection = op.get_bind()
    kinds = {row.id: row.material.get("kind") for row in connection.execute(sa.select(sources))}
    for row in connection.execute(sa.select(claims.c.id, claims.c.supports)).all():
        values = sorted(
            {kinds.get(item.get("source_id")) for item in row.supports}
            & {"web", "paper", "document"}
        )
        connection.execute(
            claims.update()
            .where(claims.c.id == row.id)
            .values(source_types="|" + "|".join(values) + "|")
        )
    op.create_table(
        "project_evidence_policy",
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("web_days", sa.Integer(), nullable=False),
        sa.Column("paper_days", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project_evidence_policy")
    op.drop_column("project_claims", "source_types")
