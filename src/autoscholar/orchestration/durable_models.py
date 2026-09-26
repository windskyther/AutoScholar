"""Database records for durable workflows; payloads never contain runtime clients."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from autoscholar.agent.database_models import Base


class WorkflowJobRow(Base):
    __tablename__ = "workflow_jobs"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    checkpoint_sequence: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pending_calls: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    usage: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowCheckpointRow(Base):
    __tablename__ = "workflow_checkpoints"
    __table_args__ = (UniqueConstraint("task_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowEventRow(Base):
    __tablename__ = "workflow_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowApprovalRow(Base):
    __tablename__ = "workflow_approvals"
    __table_args__ = (UniqueConstraint("task_id", "operation_sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    operation_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectMemoryRow(Base):
    __tablename__ = "project_memories"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ExperienceMemoryRow(Base):
    __tablename__ = "experience_memories"
    __table_args__ = (UniqueConstraint("task_id", "problem_code", "step_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    problem_code: Mapped[str] = mapped_column(String(100))
    step_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
