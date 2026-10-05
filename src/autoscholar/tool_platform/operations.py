"""Conservative deduplication for short, synchronous, fenced filesystem operations.

A committed reservation always precedes the side effect. A crash between effect and
receipt is deliberately uncertain, never automatically replayed. This is not an
atomic filesystem/database transaction or an exactly-once claim.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.orchestration.durable_repository import DurableRepository, LeaseLost
from autoscholar.tool_platform.gateway import argument_digest
from autoscholar.tool_platform.operation_models import ToolOperationRow


def failure(code: str, *, uncertain: bool = False) -> dict[str, Any]:
    return {"succeeded": False, "output": code, "error_code": code, "uncertain": uncertain}


class OperationDenied(PermissionError):
    """A known, pre-effect authorization rejection, not an I/O failure."""


class OperationStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], service: str) -> None:
        self.sessions, self.service = sessions, service

    async def authorize(
        self, session: AsyncSession, context: dict[str, Any], *, read_only: bool = False
    ) -> AgentTaskRow:
        scope = context.get("scope")
        if not isinstance(scope, dict) or not isinstance(scope.get("task_id"), str):
            raise OperationDenied("task_scope_required")
        # Inspect immutable ancestry, then lock in workflow -> child order, matching Core.
        task = await session.get(AgentTaskRow, scope["task_id"])
        if task is None:
            raise OperationDenied("task_access_denied")
        parent_id = task.parent_task_id
        if context.get("parent_task_id") != parent_id:
            raise OperationDenied("task_parent_mismatch")
        job = await session.scalar(
            select(WorkflowJobRow)
            .where(WorkflowJobRow.task_id == (parent_id or task.id))
            .with_for_update()
        )
        claim = context.get("claim")
        if job is not None:
            if (
                not isinstance(claim, dict)
                or claim.get("task_id") != job.task_id
                or not isinstance(claim.get("owner"), str)
                or type(claim.get("generation")) is not int
            ):
                raise OperationDenied("workflow_claim_required")
            try:
                DurableRepository.fence(job, claim["owner"], claim["generation"])
            except LeaseLost as exc:
                raise OperationDenied("workflow_claim_stale") from exc
            # Pause is a checkpoint-boundary request; finish the current unit.
            if not read_only and job.status not in {"running", "pause_requested"}:
                raise OperationDenied("workflow_not_running")
        elif parent_id is not None or claim is not None or task.mode == "autonomous":
            raise OperationDenied("workflow_claim_invalid")
        locked_task: AgentTaskRow | None = await session.scalar(
            select(AgentTaskRow)
            .where(AgentTaskRow.id == scope["task_id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_task is None or (not read_only and locked_task.status != "running"):
            raise OperationDenied("task_not_running")
        if scope.get("project_id") is not None and scope["project_id"] != locked_task.project_id:
            raise OperationDenied("task_project_mismatch")
        if float(context["deadline"]) <= time.time():
            raise OperationDenied("operation_expired")
        return locked_task

    async def lookup(self, operation_id: str, context: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self.sessions() as session:
                task = await self.authorize(session, context, read_only=True)
                row = await session.get(ToolOperationRow, operation_id)
                if row is None or row.task_id != task.id or row.service != self.service:
                    return {"status": "missing", "result": None}
                return {
                    "status": row.status,
                    "result": row.result if row.status == "completed" else None,
                }
        except OperationDenied:
            return {"status": "denied", "result": None}

    def receipt(self, row: ToolOperationRow, context: dict[str, Any]) -> dict[str, Any]:
        if (
            row.service != self.service
            or row.task_id != context["scope"]["task_id"]
            or row.tool != context["tool"]
            or row.arguments_sha256 != context["arguments_sha256"]
            or row.authority_sha256 != self.authority(context)
        ):
            return failure("operation_identity_conflict")
        return (
            row.result
            if row.status == "completed" and row.result is not None
            else failure("operation_uncertain", uncertain=True)
        )

    @staticmethod
    def authority(context: dict[str, Any]) -> str:
        return argument_digest(
            {key: context.get(key) for key in ("scope", "claim", "parent_task_id")}
        )

    async def execute(
        self, context: dict[str, Any], action: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        try:
            async with self.sessions() as session:
                await self.authorize(session, context)
                existing = await session.get(ToolOperationRow, context["operation_id"])
                if existing is not None:
                    return self.receipt(existing, context)
                session.add(
                    ToolOperationRow(
                        id=context["operation_id"],
                        task_id=context["scope"]["task_id"],
                        service=self.service,
                        tool=context["tool"],
                        arguments_sha256=context["arguments_sha256"],
                        authority_sha256=self.authority(context),
                        status="running",
                        result=None,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await session.get(ToolOperationRow, context["operation_id"])
                    if existing is None:
                        raise
                    return self.receipt(existing, context)
            async with self.sessions() as session:
                # Recheck after reservation: cancellation/lease takeover may have won.
                await self.authorize(session, context)
                row = await session.get(ToolOperationRow, context["operation_id"])
                assert row is not None
                # Caller must perform only bounded synchronous workspace work here.
                # Holding the task and workflow locks serializes quota checks and fences writes.
                result = await action()
                row.result, row.status = result, "completed"
                await session.commit()
                return result
        except OperationDenied as exc:
            return failure(str(exc))
