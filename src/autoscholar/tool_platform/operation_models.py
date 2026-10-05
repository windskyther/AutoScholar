"""Durable service-side operation receipts; no secrets or model prompts are stored."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from autoscholar.agent.database_models import Base


class ToolOperationRow(Base):
    __tablename__ = "tool_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    service: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(64))
    arguments_sha256: Mapped[str] = mapped_column(String(64))
    authority_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CoreToolCallRow(Base):
    __tablename__ = "core_tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    service: Mapped[str] = mapped_column(String(32))
    tool: Mapped[str] = mapped_column(String(64))
    arguments_sha256: Mapped[str] = mapped_column(String(64))
    authority_sha256: Mapped[str] = mapped_column(String(64))
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
