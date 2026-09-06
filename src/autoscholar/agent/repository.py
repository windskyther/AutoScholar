from collections.abc import Sequence
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from autoscholar.agent.database_models import AgentTaskRow, ToolCallRow
from autoscholar.agent.records import AgentTaskRecord, TaskStatus, ToolCallStatus, ToolTraceRecord


class AgentTaskRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create_task(self, *, task_id: str, objective: str) -> AgentTaskRecord:
        async with self._sessions() as session:
            row = AgentTaskRow(
                id=task_id,
                status="running",
                objective=objective,
                plan=[],
                metrics={},
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._task_record(row, ())

    async def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        plan: list[str],
        answer: str | None,
        metrics: dict[str, int],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AgentTaskRecord:
        async with self._sessions() as session:
            row = await session.get(AgentTaskRow, task_id)
            if row is None:
                raise LookupError(f"Unknown agent task: {task_id}")
            row.status = status
            row.plan = plan
            row.answer = answer
            row.metrics = metrics
            row.error_code = error_code
            row.error_message = error_message
            await session.commit()
        record = await self.get_task(task_id)
        if record is None:  # pragma: no cover - protected by the transaction above
            raise LookupError(f"Unknown agent task: {task_id}")
        return record

    async def add_tool_call(
        self,
        *,
        task_id: str,
        sequence: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        status: ToolCallStatus,
        duration_ms: float,
        error_code: str | None = None,
    ) -> ToolTraceRecord:
        async with self._sessions() as session:
            row = ToolCallRow(
                id=str(uuid4()),
                task_id=task_id,
                sequence=sequence,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                output=output,
                status=status,
                error_code=error_code,
                duration_ms=duration_ms,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._tool_record(row)

    async def get_task(self, task_id: str) -> AgentTaskRecord | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(AgentTaskRow)
                .where(AgentTaskRow.id == task_id)
                .options(selectinload(AgentTaskRow.tool_calls))
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._task_record(row, row.tool_calls)

    @classmethod
    def _task_record(
        cls, row: AgentTaskRow, tool_calls: Sequence[ToolCallRow]
    ) -> AgentTaskRecord:
        return AgentTaskRecord(
            id=row.id,
            status=cast(TaskStatus, row.status),
            objective=row.objective,
            plan=list(row.plan),
            answer=row.answer,
            metrics=dict(row.metrics),
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
            tool_calls=[cls._tool_record(call) for call in tool_calls],
        )

    @staticmethod
    def _tool_record(row: ToolCallRow) -> ToolTraceRecord:
        return ToolTraceRecord(
            id=row.id,
            task_id=row.task_id,
            sequence=row.sequence,
            call_id=row.call_id,
            tool_name=row.tool_name,
            arguments=dict(row.arguments),
            output=row.output,
            status=cast(ToolCallStatus, row.status),
            error_code=row.error_code,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )
