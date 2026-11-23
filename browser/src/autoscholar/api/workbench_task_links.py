"""Browser-facing navigation and editing of task references."""

from pydantic import BaseModel
from sqlalchemy import delete, func, select

from autoscholar.agent.database_models import AgentTaskRow, TaskLinkRow
from autoscholar.agent.records import TaskStatus
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow
from autoscholar.orchestration.task_links import (
    ENDED,
    ReferenceIds,
    load_reference_rows,
    reference_eligible,
    reference_error,
    task_deleted,
)
from autoscholar.orchestration.task_types import TASK_TYPES
from autoscholar.rag.database_models import ProjectRow


class LinkedTask(BaseModel):
    task_id: str
    objective: str
    status: TaskStatus


class LinkedTaskPage(BaseModel):
    items: list[LinkedTask]
    total: int
    limit: int
    offset: int


class TaskLinks(BaseModel):
    project_name: str
    previous: LinkedTask | None
    references: list[LinkedTask]
    followups: LinkedTaskPage
    can_append: bool
    model_profile_id: str | None
    workflow_kind: str | None


class TaskReferencesRequest(BaseModel):
    task_ids: ReferenceIds


def summary(task: AgentTaskRow) -> LinkedTask:
    return LinkedTask.model_validate(
        {"task_id": task.id, "objective": task.objective[:500], "status": task.status}
    )


async def candidates(
    store: WorkbenchRepository, project_id: str, query: str | None
) -> list[LinkedTask]:
    async with store.sessions() as session:
        project = await session.get(ProjectRow, project_id)
        if not project or project.deleted_at is not None:
            raise AppError(status_code=404, code="project_not_found", message="Project not found")
        stmt = select(AgentTaskRow).where(
            AgentTaskRow.project_id == project_id,
            AgentTaskRow.parent_task_id.is_(None),
            reference_eligible(),
            ~task_deleted(AgentTaskRow.id),
        )
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(AgentTaskRow.objective.ilike(f"%{escaped}%", escape="\\"))
        rows = await session.scalars(
            stmt.order_by(AgentTaskRow.created_at.desc(), AgentTaskRow.id.desc()).limit(40)
        )
        return [summary(task) for task in rows]


async def view(store: WorkbenchRepository, task_id: str, limit: int, offset: int) -> TaskLinks:
    async with store.sessions() as session:
        task = await store.root(session, task_id)
        project = await session.get(ProjectRow, task.project_id) if task.project_id else None
        if not project:
            raise AppError(status_code=404, code="project_not_found", message="Project not found")
        outgoing = await session.execute(
            select(TaskLinkRow.kind, AgentTaskRow)
            .join(AgentTaskRow, TaskLinkRow.source_task_id == AgentTaskRow.id)
            .where(
                TaskLinkRow.task_id == task_id,
                AgentTaskRow.project_id == task.project_id,
                ~task_deleted(AgentTaskRow.id),
            )
            .order_by(TaskLinkRow.position)
        )
        previous = None
        references = []
        for kind, source in outgoing:
            if kind == "continuation":
                previous = summary(source)
            else:
                references.append(summary(source))
        followups = (
            select(AgentTaskRow)
            .join(TaskLinkRow, TaskLinkRow.task_id == AgentTaskRow.id)
            .where(
                TaskLinkRow.source_task_id == task_id,
                TaskLinkRow.kind == "continuation",
                AgentTaskRow.project_id == task.project_id,
                ~task_deleted(AgentTaskRow.id),
            )
        )
        total = await session.scalar(select(func.count()).select_from(followups.subquery()))
        rows = await session.scalars(
            followups.order_by(AgentTaskRow.created_at.desc(), AgentTaskRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        payload = (
            await session.scalar(
                select(WorkflowCheckpointRow.payload)
                .join(
                    WorkflowJobRow,
                    (WorkflowJobRow.task_id == WorkflowCheckpointRow.task_id)
                    & (WorkflowJobRow.checkpoint_sequence == WorkflowCheckpointRow.sequence),
                )
                .where(WorkflowJobRow.task_id == task_id)
            )
            or {}
        )
        kind = payload.get("workflow_kind")
        return TaskLinks(
            project_name=project.name,
            previous=previous,
            references=references,
            followups=LinkedTaskPage(
                items=[summary(item) for item in rows], total=total or 0, limit=limit, offset=offset
            ),
            can_append=task.mode == "autonomous" and task.status in ENDED,
            model_profile_id=payload.get("model_profile_id"),
            workflow_kind=kind if isinstance(kind, str) and kind in TASK_TYPES else None,
        )


async def save_references(
    store: WorkbenchRepository, task_id: str, reference_ids: list[str]
) -> list[LinkedTask]:
    async with store.sessions.begin() as session:
        task = await store.root(session, task_id)
        project = await session.scalar(
            select(ProjectRow)
            .where(ProjectRow.id == task.project_id, ProjectRow.deleted_at.is_(None))
            .with_for_update()
        )
        if not project:
            raise AppError(status_code=404, code="project_not_found", message="Project not found")
        await store.root(session, task_id)
        previous = await session.scalar(
            select(TaskLinkRow.source_task_id).where(
                TaskLinkRow.task_id == task_id, TaskLinkRow.kind == "continuation"
            )
        )
        if previous in reference_ids:
            raise reference_error()
        sources = await load_reference_rows(session, project.id, task_id, reference_ids)
        await session.execute(
            delete(TaskLinkRow).where(
                TaskLinkRow.task_id == task_id, TaskLinkRow.kind == "reference"
            )
        )
        session.add_all(
            TaskLinkRow(task_id=task_id, source_task_id=source, kind="reference", position=index)
            for index, source in enumerate(reference_ids, start=1)
        )
        return [summary(sources[source]) for source in reference_ids]
