"""Allowlisted security metadata only; bodies, raw paths and headers are never accepted."""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from autoscholar.agent.database_models import Base


@dataclass(frozen=True)
class AuditEvent:
    id: str
    request_id: str
    actor: str
    category: str
    method: str
    phase: str
    outcome: str
    status: int | None = None
    task_id: str | None = None
    project_id: str | None = None
    operation_id: str | None = None
    usage: dict[str, int] | None = None
    action: str = "request"


current_security_audit: ContextVar[Callable[[str, str, bool], Awaitable[None]] | None] = ContextVar(
    "security_audit", default=None
)


class AuditSink(Protocol):
    async def write(self, event: AuditEvent) -> None: ...


class SecurityAuditRow(Base):
    __tablename__ = "security_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16))
    method: Mapped[str] = mapped_column(String(8))
    phase: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(64))
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage: Mapped[dict[str, int] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DatabaseAuditSink:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def write(self, event: AuditEvent) -> None:
        async with self.sessions() as session, session.begin():
            session.add(SecurityAuditRow(**asdict(event)))


class UnavailableAuditSink:
    async def write(self, event: AuditEvent) -> None:
        raise RuntimeError("Audit storage unavailable")
