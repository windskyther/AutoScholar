"""Core-owned invocation identities, without raw requests, credentials or prompts."""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.tool_platform.gateway import argument_digest, validate
from autoscholar.tool_platform.operation_models import CoreToolCallRow, ToolOperationRow
from autoscholar.tool_platform.operations import OperationStore


class InvocationRegistry:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def prepare(self, service: str, context: dict[str, Any], schema: dict[str, Any]) -> None:
        scope = context.get("scope")
        if scope is None:
            return
        async with self.sessions() as session:
            await OperationStore(self.sessions, service).authorize(session, context)
            row = await session.get(CoreToolCallRow, context["operation_id"])
            if row is not None:
                raise ValueError("Core invocation ID already exists; use receipt lookup")
            session.add(
                CoreToolCallRow(
                    id=context["operation_id"],
                    task_id=scope["task_id"],
                    parent_task_id=context.get("parent_task_id"),
                    service=service,
                    tool=context["tool"],
                    arguments_sha256=context["arguments_sha256"],
                    authority_sha256=OperationStore.authority(context),
                    output_schema=schema,
                    status="prepared",
                    result=None,
                    result_sha256=None,
                )
            )
            await session.commit()

    async def complete(self, context: dict[str, Any], result: dict[str, Any]) -> None:
        if context.get("scope") is None:
            return
        async with self.sessions() as session:
            row = await session.get(CoreToolCallRow, context["operation_id"])
            if row is None:
                raise ValueError("Unknown Core invocation")
            await OperationStore(self.sessions, row.service).authorize(session, context)
            if row.arguments_sha256 != context["arguments_sha256"]:
                raise ValueError("Core invocation changed")
            row.result, row.result_sha256, row.status = result, argument_digest(result), "completed"
            await session.commit()

    async def inspect(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CoreToolCallRow)
                    .where(
                        or_(
                            CoreToolCallRow.task_id == task_id,
                            CoreToolCallRow.parent_task_id == task_id,
                        )
                    )
                    .order_by(CoreToolCallRow.created_at.desc(), CoreToolCallRow.id)
                    .limit(limit)
                )
            ).all()
            return [
                {
                    "operation_id": row.id,
                    "task_id": row.task_id,
                    "service": row.service,
                    "tool": row.tool,
                    "arguments_sha256": row.arguments_sha256,
                    "core_status": row.status,
                    "receipt_status": await self.receipt_status(session, row),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    @staticmethod
    async def receipt_status(session: AsyncSession, row: CoreToolCallRow) -> str:
        result = row.result
        if row.status == "completed":
            if result is None or argument_digest(result) != row.result_sha256:
                return "conflict"
        else:
            receipt = await session.get(ToolOperationRow, row.id)
            if receipt is None:
                return "missing"
            if (
                receipt.task_id != row.task_id
                or receipt.service != row.service
                or receipt.tool != row.tool
                or receipt.arguments_sha256 != row.arguments_sha256
                or receipt.authority_sha256 != row.authority_sha256
            ):
                return "conflict"
            if receipt.status != "completed" or receipt.result is None:
                return "uncertain"
            result = receipt.result
        try:
            validate(row.output_schema, result, "tool_result_invalid")
        except Exception:
            return "conflict"
        return "uncertain" if result.get("uncertain") else "completed"
