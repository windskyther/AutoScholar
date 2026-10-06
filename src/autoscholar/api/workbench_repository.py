"""Read-only, paginated views over a root task and its same-project direct children."""

from dataclasses import asdict
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.agent.database_models import (
    AgentTaskRow,
    ArtifactRow,
    EvidenceRow,
    ExperimentRow,
    TaskPlanRow,
)
from autoscholar.agent.records import ResolvedAgentMode, TaskStatus
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.workbench_events import public_event
from autoscholar.api.workbench_models import (
    ChildTaskPage,
    ExecutionSummary,
    FamilyArtifactResponse,
    FamilyEvidenceResponse,
    FamilyExperimentResponse,
    PlanSummary,
    ResourceCounts,
    TaskListResponse,
    TaskOverviewResponse,
    TaskSummary,
    WorkflowEventPage,
)
from autoscholar.core.budget import BudgetLimits
from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable_models import (
    WorkflowCheckpointRow,
    WorkflowEventRow,
    WorkflowJobRow,
)
from autoscholar.rag.database_models import ProjectRow


class WorkbenchRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def summary(row: AgentTaskRow) -> TaskSummary:
        return TaskSummary.model_validate(
            {
                "task_id": row.id,
                "project_id": row.project_id,
                "parent_task_id": row.parent_task_id,
                "objective": row.objective,
                "mode": row.mode,
                "status": row.status,
                "metrics": row.metrics,
                "error_code": row.error_code,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )

    @staticmethod
    async def root(session: AsyncSession, task_id: str) -> AgentTaskRow:
        row = await session.get(AgentTaskRow, task_id)
        if row is None or row.parent_task_id is not None:
            raise AppError(
                status_code=404, code="workbench_task_not_found", message="Root task was not found"
            )
        return row

    @staticmethod
    def family(root: AgentTaskRow) -> Any:
        return select(AgentTaskRow.id).where(
            AgentTaskRow.project_id == root.project_id,
            or_(AgentTaskRow.id == root.id, AgentTaskRow.parent_task_id == root.id),
        )

    async def tasks(
        self,
        project_id: str,
        *,
        status: TaskStatus | None,
        mode: ResolvedAgentMode | None,
        query: str | None,
        limit: int,
        offset: int,
    ) -> TaskListResponse:
        async with self.sessions() as session:
            if await session.get(ProjectRow, project_id) is None:
                raise AppError(
                    status_code=404, code="project_not_found", message="Project was not found"
                )
            conditions = [
                AgentTaskRow.project_id == project_id,
                AgentTaskRow.parent_task_id.is_(None),
            ]
            if status is not None:
                conditions.append(AgentTaskRow.status == status)
            if mode is not None:
                conditions.append(AgentTaskRow.mode == mode)
            if query:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                conditions.append(AgentTaskRow.objective.ilike(f"%{escaped}%", escape="\\"))
            total = await session.scalar(
                select(func.count()).select_from(AgentTaskRow).where(*conditions)
            )
            rows = (
                await session.scalars(
                    select(AgentTaskRow)
                    .where(*conditions)
                    .order_by(AgentTaskRow.created_at.desc(), AgentTaskRow.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return TaskListResponse(
                project_id=project_id,
                items=[self.summary(r) for r in rows],
                total=int(total or 0),
                limit=limit,
                offset=offset,
            )

    async def overview(self, task_id: str, *, limit: int, offset: int) -> TaskOverviewResponse:
        async with self.sessions() as session:
            root = await self.root(session, task_id)
            child_filter = (
                AgentTaskRow.parent_task_id == task_id,
                AgentTaskRow.project_id == root.project_id,
            )
            total = await session.scalar(
                select(func.count()).select_from(AgentTaskRow).where(*child_filter)
            )
            children = (
                await session.scalars(
                    select(AgentTaskRow)
                    .where(*child_filter)
                    .order_by(AgentTaskRow.created_at, AgentTaskRow.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            plan = await session.scalar(
                select(TaskPlanRow)
                .where(TaskPlanRow.task_id == task_id)
                .order_by(TaskPlanRow.version.desc())
                .limit(1)
            )
            job = await session.get(WorkflowJobRow, task_id)
            execution = None
            if job is not None:
                checkpoint = await session.scalar(
                    select(WorkflowCheckpointRow).where(
                        WorkflowCheckpointRow.task_id == task_id,
                        WorkflowCheckpointRow.sequence == job.checkpoint_sequence,
                    )
                )
                payload = checkpoint.payload if checkpoint else {}
                execution = ExecutionSummary.model_validate(
                    {
                        "status": job.status,
                        "stage": payload.get("stage"),
                        "plan_version": payload.get("version"),
                        "checkpoint_sequence": job.checkpoint_sequence,
                        "budget_used": {
                            k: v
                            for k, v in job.usage.items()
                            if k in {*BudgetLimits.model_fields, "input_tokens", "output_tokens"}
                        },
                        "budget_limits": payload.get("limits"),
                        "active_seconds": job.active_seconds,
                        "pending_call_count": len(job.pending_calls),
                        "error_code": job.error_code,
                    }
                )
            counts = {}
            for name, table in (
                ("evidence", EvidenceRow),
                ("experiments", ExperimentRow),
                ("artifacts", ArtifactRow),
            ):
                count = await session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.task_id.in_(self.family(root)))
                )
                counts[name] = int(count or 0)
            return TaskOverviewResponse(
                task=self.summary(root),
                children=ChildTaskPage(
                    items=[self.summary(r) for r in children],
                    total=int(total or 0),
                    limit=limit,
                    offset=offset,
                ),
                current_plan=PlanSummary.model_validate(
                    {"version": plan.version, "plan": plan.payload}
                )
                if plan
                else None,
                execution=execution,
                resources=ResourceCounts(**counts),
                answer=root.answer[:262144] if root.answer is not None else None,
                answer_truncated=len(root.answer or "") > 262144,
            )

    async def resources(
        self,
        task_id: str,
        kind: str,
        *,
        limit: int,
        offset: int,
    ) -> FamilyEvidenceResponse | FamilyExperimentResponse | FamilyArtifactResponse:
        tables: dict[str, type[EvidenceRow] | type[ExperimentRow] | type[ArtifactRow]] = {
            "evidence": EvidenceRow,
            "experiments": ExperimentRow,
            "artifacts": ArtifactRow,
        }
        table = tables[kind]
        async with self.sessions() as session:
            root = await self.root(session, task_id)
            condition = table.task_id.in_(self.family(root))
            total = await session.scalar(select(func.count()).select_from(table).where(condition))
            rows = (
                await session.scalars(
                    select(table)
                    .where(condition)
                    .order_by(table.created_at, table.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            payload: dict[str, Any] = {
                "task_id": task_id,
                "total": int(total or 0),
                "limit": limit,
                "offset": offset,
                "items": [],
            }
            for row in rows:
                if isinstance(row, EvidenceRow):
                    payload["items"].append(
                        asdict(AgentTaskRepository._evidence_record(row)) | {"task_id": row.task_id}
                    )
                elif isinstance(row, ExperimentRow):
                    payload["items"].append(asdict(AgentTaskRepository._experiment_record(row)))
                elif isinstance(row, ArtifactRow):
                    payload["items"].append(asdict(AgentTaskRepository._artifact_record(row)))
            responses: dict[
                str,
                type[FamilyEvidenceResponse]
                | type[FamilyExperimentResponse]
                | type[FamilyArtifactResponse],
            ] = {
                "evidence": FamilyEvidenceResponse,
                "experiments": FamilyExperimentResponse,
                "artifacts": FamilyArtifactResponse,
            }
            return responses[kind].model_validate(payload)

    async def events(
        self,
        task_id: str,
        *,
        after: int | None = None,
        before: int | None = None,
        limit: int = 50,
    ) -> WorkflowEventPage:
        async with self.sessions() as session:
            root = await self.root(session, task_id)
            latest = root.event_sequence
            if (after is not None and after > latest) or (
                before is not None and before > latest + 1
            ):
                raise AppError(
                    status_code=409,
                    code="event_cursor_invalid",
                    message="Reload event history before resuming",
                )
            job = await session.get(WorkflowJobRow, task_id)
            query = select(WorkflowEventRow).where(
                WorkflowEventRow.task_id == task_id, WorkflowEventRow.sequence <= latest
            )
            forward = after is not None
            if forward:
                query = query.where(WorkflowEventRow.sequence > after)
            elif before is not None:
                query = query.where(WorkflowEventRow.sequence < before)
            query = query.order_by(
                WorkflowEventRow.sequence.asc() if forward else WorkflowEventRow.sequence.desc()
            ).limit(limit + 1)
            rows = list((await session.scalars(query)).all())
            more = len(rows) > limit
            rows = rows[:limit]
            if not forward:
                rows.reverse()
            cursor = rows[-1].sequence if rows else (after or 0)
            return WorkflowEventPage(
                task_id=task_id,
                status=cast(TaskStatus, root.status),
                durable=job is not None,
                items=[public_event(row) for row in rows],
                next_cursor=cursor,
                has_more=more if forward else bool(rows and cursor < latest),
                has_older=(bool(rows and rows[0].sequence > 1) if forward else more),
            )
