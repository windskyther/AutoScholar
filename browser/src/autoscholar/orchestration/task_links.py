"""Explicit task links and bounded copies of results included in a submission."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import SQLColumnExpression, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from autoscholar.agent.database_models import AgentTaskRow, TaskLinkRow
from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable_models import WorkflowEventRow

TaskId = Annotated[str, StringConstraints(min_length=1, max_length=36)]
ReferenceIds = Annotated[list[TaskId], Field(max_length=5)]
ENDED = frozenset({"succeeded", "partial", "failed", "budget_exceeded", "cancelled"})


class PriorTaskResult(BaseModel):
    task_id: TaskId
    kind: Literal["continuation", "reference"]
    objective: str = Field(max_length=1000)
    status: str
    answer_excerpt: str | None = Field(max_length=4000)


def task_deleted(task_id: str | SQLColumnExpression[str]) -> ColumnElement[bool]:
    return (
        select(WorkflowEventRow.id)
        .where(WorkflowEventRow.task_id == task_id, WorkflowEventRow.kind == "task_deleted")
        .exists()
    )


def reference_eligible() -> ColumnElement[bool]:
    return AgentTaskRow.status.in_(ENDED) & (func.trim(AgentTaskRow.answer) != "")


def reference_error() -> AppError:
    return AppError(
        status_code=422,
        code="task_reference_unavailable",
        message="Select distinct, completed tasks with output from the same project",
    )


async def load_reference_rows(
    session: AsyncSession,
    project_id: str | None,
    task_id: str,
    reference_ids: list[str],
    continuation_id: str | None = None,
) -> dict[str, AgentTaskRow]:
    ids = ([continuation_id] if continuation_id else []) + reference_ids
    if not ids:
        return {}
    if not project_id or task_id in ids or len(ids) != len(set(ids)) or len(reference_ids) > 5:
        raise reference_error()
    rows = {
        row.id: row
        for row in await session.scalars(
            select(AgentTaskRow).where(
                AgentTaskRow.id.in_(ids),
                AgentTaskRow.project_id == project_id,
                AgentTaskRow.parent_task_id.is_(None),
                AgentTaskRow.status.in_(ENDED),
                ~task_deleted(AgentTaskRow.id),
            )
        )
    }
    if continuation_id and (
        continuation_id not in rows or rows[continuation_id].mode != "autonomous"
    ):
        raise AppError(
            status_code=422,
            code="task_continuation_unavailable",
            message="The preceding research task must be finished and in the same project",
        )
    if any(item not in rows or not (rows[item].answer or "").strip() for item in reference_ids):
        raise reference_error()
    return rows


def result_excerpt(answer: str | None) -> str | None:
    if answer is None or len(answer) <= 4000:
        return answer
    return answer[:1990] + "\n[...omitted...]\n" + answer[-1990:]


async def prepare_submission_links(
    session: AsyncSession,
    project_id: str | None,
    task_id: str,
    reference_ids: list[str],
    continuation_id: str | None,
) -> tuple[list[TaskLinkRow], list[PriorTaskResult]]:
    rows = await load_reference_rows(session, project_id, task_id, reference_ids, continuation_id)
    links: list[TaskLinkRow] = []
    results: list[PriorTaskResult] = []
    for position, (source_id, kind) in enumerate(
        ([(continuation_id, "continuation")] if continuation_id else [])
        + [(source, "reference") for source in reference_ids]
    ):
        row = rows[source_id]
        links.append(
            TaskLinkRow(task_id=task_id, source_task_id=source_id, kind=kind, position=position)
        )
        results.append(
            PriorTaskResult(
                task_id=source_id,
                kind="continuation" if kind == "continuation" else "reference",
                objective=row.objective[:1000],
                status=row.status,
                answer_excerpt=result_excerpt(row.answer),
            )
        )
    return links, results
