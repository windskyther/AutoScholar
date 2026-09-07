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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AgentTaskRow(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    objective: Mapped[str] = mapped_column(Text)
    plan: Mapped[list[str]] = mapped_column(JSON, default=list)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(16), default="compute")
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    tool_calls: Mapped[list["ToolCallRow"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ToolCallRow.sequence",
    )
    evidence: Mapped[list["EvidenceRow"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="EvidenceRow.citation_key",
    )


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    call_id: Mapped[str] = mapped_column(String(255))
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    task: Mapped[AgentTaskRow] = relationship(back_populates="tool_calls")


class EvidenceRow(Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("task_id", "citation_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    citation_key: Mapped[str] = mapped_column(String(16))
    source_type: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    query: Mapped[str] = mapped_column(String(400))
    topic: Mapped[str] = mapped_column(String(255))
    claim: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[str] = mapped_column(Text)
    relevance: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    task: Mapped[AgentTaskRow] = relationship(back_populates="evidence")
