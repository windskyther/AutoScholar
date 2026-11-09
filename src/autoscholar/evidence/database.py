"""Independent evidence records; task reports and historical checkpoints stay intact."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from autoscholar.agent.database_models import Base


class SourceVersionRow(Base):
    __tablename__ = "project_source_versions"
    __table_args__ = (UniqueConstraint("project_id", "identity_hash", "content_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    identity_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    material: Mapped[dict[str, Any]] = mapped_column(JSON)
    document_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectClaimRow(Base):
    __tablename__ = "project_claims"
    __table_args__ = (UniqueConstraint("task_id", "ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(Text)
    supports: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list)
    review_status: Mapped[str] = mapped_column(String(16), default="ready")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    audit_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indexed_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    indexed_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidencePublicationRow(Base):
    __tablename__ = "project_evidence_publications"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    checkpoint_sequence: Mapped[int] = mapped_column(Integer)
    audit_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceReuseRow(Base):
    __tablename__ = "project_evidence_reuse"
    __table_args__ = (UniqueConstraint("task_id", "claim_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    claim_id: Mapped[str] = mapped_column(
        ForeignKey("project_claims.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="selected")
    reason: Mapped[str] = mapped_column(Text, default="")
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
