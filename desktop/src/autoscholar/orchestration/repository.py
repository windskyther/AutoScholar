from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.agent.database_models import TaskPlanRow, TaskReviewRow, TaskStepRow
from autoscholar.orchestration.models import ReviewResult, TaskPlan


class WorkflowRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_plan(self, task_id: str, version: int, plan: TaskPlan, reason: str) -> None:
        async with self.sessions() as session:
            session.add(
                TaskPlanRow(
                    id=str(uuid4()),
                    task_id=task_id,
                    version=version,
                    payload=plan.model_dump(),
                    reason=reason,
                )
            )
            await session.commit()

    async def start_step(self, task_id: str, version: int, step_id: str, child_task_id: str) -> str:
        run_id = str(uuid4())
        async with self.sessions() as session:
            session.add(
                TaskStepRow(
                    id=run_id,
                    task_id=task_id,
                    plan_version=version,
                    step_id=step_id,
                    child_task_id=child_task_id,
                    status="running",
                    result={},
                )
            )
            await session.commit()
        return run_id

    async def finish_step(self, run_id: str, status: str, result: dict[str, Any]) -> None:
        async with self.sessions() as session:
            row = await session.get(TaskStepRow, run_id)
            if row is None:
                raise LookupError("Unknown workflow step")
            row.status = status
            row.result = result
            await session.commit()

    async def save_review(self, task_id: str, version: int, review: ReviewResult) -> None:
        async with self.sessions() as session:
            session.add(
                TaskReviewRow(
                    id=str(uuid4()),
                    task_id=task_id,
                    plan_version=version,
                    payload=review.model_dump(),
                )
            )
            await session.commit()

    async def history(
        self, task_id: str, kind: Literal["plans", "steps", "reviews"]
    ) -> list[dict[str, Any]]:
        tables: dict[str, type[TaskPlanRow] | type[TaskStepRow] | type[TaskReviewRow]] = {
            "plans": TaskPlanRow,
            "steps": TaskStepRow,
            "reviews": TaskReviewRow,
        }
        table = tables[kind]
        async with self.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(table)
                        .where(table.task_id == task_id)
                        .order_by(table.created_at, table.id)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    column.name: (
                        getattr(row, column.name).isoformat()
                        if column.name == "created_at"
                        else getattr(row, column.name)
                    )
                    for column in table.__table__.columns
                }
                for row in rows
            ]
