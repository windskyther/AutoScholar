"""Paginated task views and removal of completed goals from the workbench."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

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
    WorkbenchApproval,
    WorkbenchApprovalPage,
    WorkbenchControlState,
    WorkflowEventPage,
)
from autoscholar.core.budget import BudgetLimits
from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable_models import (
    WorkflowApprovalRow,
    WorkflowCheckpointRow,
    WorkflowEventRow,
    WorkflowJobRow,
)
from autoscholar.orchestration.durable_repository import utc
from autoscholar.rag.database_models import ProjectRow
from autoscholar.tool_platform.operation_models import CoreToolCallRow


class WorkbenchRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def deleted(task_id: Any) -> Any:
        # A durable removal event hides a goal without erasing execution receipts.
        return (
            select(WorkflowEventRow.id)
            .where(
                WorkflowEventRow.task_id == task_id,
                WorkflowEventRow.kind == "task_deleted",
            )
            .exists()
        )

    async def delete_task(self, project_id: str, task_id: str) -> None:
        terminal = {"succeeded", "partial", "failed", "budget_exceeded", "cancelled"}
        async with self.sessions() as session:
            project = await session.scalar(
                select(ProjectRow).where(ProjectRow.id == project_id).with_for_update()
            )
            if project is None or project.deleted_at is not None:
                raise AppError(
                    status_code=404, code="project_not_found", message="Project not found"
                )
            # Follow the workflow's job-before-event/task lock order.
            root_job = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.task_id == task_id).with_for_update()
            )
            root = await session.scalar(
                select(AgentTaskRow)
                .where(
                    AgentTaskRow.id == task_id,
                    AgentTaskRow.project_id == project_id,
                    AgentTaskRow.parent_task_id.is_(None),
                )
                .with_for_update()
            )
            if root is None:
                raise AppError(
                    status_code=404, code="workbench_task_not_found", message="Root task not found"
                )
            if await session.scalar(select(self.deleted(task_id))):
                return
            family = (
                select(AgentTaskRow.id)
                .where(AgentTaskRow.id == task_id)
                .cte("removed_task_family", recursive=True)
            )
            family = family.union(
                select(AgentTaskRow.id).join(family, AgentTaskRow.parent_task_id == family.c.id)
            )
            ids = select(family.c.id)
            tasks = (
                await session.scalars(select(AgentTaskRow).where(AgentTaskRow.id.in_(ids)))
            ).all()
            jobs = (
                await session.scalars(select(WorkflowJobRow).where(WorkflowJobRow.task_id.in_(ids)))
            ).all()
            states = {item.task_id: item.status for item in jobs}
            unknown = await session.scalar(
                select(func.count(CoreToolCallRow.id)).where(
                    CoreToolCallRow.task_id.in_(ids), CoreToolCallRow.status != "completed"
                )
            )
            stopped = (
                root_job is not None
                and root_job.status in terminal
                and root_job.owner is None
                and root_job.lease_until is None
            )
            if stopped:
                # Removal only hides the workbench goal. A fenced, terminal root
                # cannot dispatch again; retain its uncertain receipts and stale
                # child states exactly as recorded. Independent child jobs must
                # also be stopped before hiding their parent.
                blocked = any(
                    item.status not in terminal
                    or item.owner is not None
                    or item.lease_until is not None
                    for item in jobs
                )
            else:
                blocked = bool(
                    unknown
                    or any(
                        item.status not in terminal
                        or item.pending_calls
                        or item.owner is not None
                        or item.lease_until is not None
                        for item in jobs
                    )
                    or any(states.get(item.id, item.status) not in terminal for item in tasks)
                )
            if blocked:
                raise AppError(
                    status_code=409,
                    code="task_has_active_operations",
                    message="Finish or reconcile task operations before deletion",
                )
            root.event_sequence += 1
            session.add(
                WorkflowEventRow(
                    id=str(uuid4()),
                    task_id=task_id,
                    sequence=root.event_sequence,
                    kind="task_deleted",
                    payload={},
                )
            )
            await session.commit()

    async def markdown(self, task_id: str) -> tuple[str, str]:
        async with self.sessions() as session:
            root = await self.root(session, task_id)
            if not root.answer:
                raise AppError(
                    status_code=409,
                    code="task_output_unavailable",
                    message="Task output is not available",
                )
            return root.objective, root.answer

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
    async def visible_project(session: AsyncSession, project_id: str | None) -> None:
        if project_id is not None:
            project = await session.get(ProjectRow, project_id)
            if project is not None and project.deleted_at is not None:
                raise AppError(
                    status_code=404, code="project_not_found", message="Project was not found"
                )

    @staticmethod
    async def root(session: AsyncSession, task_id: str) -> AgentTaskRow:
        row = await session.get(AgentTaskRow, task_id)
        if (
            row is None
            or row.parent_task_id is not None
            or await session.scalar(select(WorkbenchRepository.deleted(task_id)))
        ):
            raise AppError(
                status_code=404, code="workbench_task_not_found", message="Root task was not found"
            )
        await WorkbenchRepository.visible_project(session, row.project_id)
        return row

    async def controls(self, task_id: str) -> WorkbenchControlState:
        async with self.sessions() as session:
            # One statement gives the UI a coherent state token; mutations validate
            # it again under locks, never trusting UI button availability.
            result = (
                await session.execute(
                    select(AgentTaskRow, WorkflowJobRow)
                    .outerjoin(WorkflowJobRow, WorkflowJobRow.task_id == AgentTaskRow.id)
                    .where(
                        AgentTaskRow.id == task_id,
                        AgentTaskRow.parent_task_id.is_(None),
                        ~self.deleted(AgentTaskRow.id),
                    )
                )
            ).first()
            if result is None:
                raise AppError(
                    status_code=404,
                    code="workbench_task_not_found",
                    message="Root task was not found",
                )
            task, job = result
            await self.visible_project(session, task.project_id)
            if job is None:
                return WorkbenchControlState(
                    task_id=task_id, status=task.status, expected=None, actions=[]
                )
            actions = []
            if job.status in {"queued", "running"}:
                actions.append("pause")
            if job.status == "paused":
                actions.append("resume")
            if job.status in {
                "queued",
                "running",
                "paused",
                "pause_requested",
                "awaiting_approval",
                "recovery_required",
            }:
                actions.append("cancel")
            return WorkbenchControlState.model_validate(
                {
                    "task_id": task_id,
                    "status": job.status,
                    "actions": actions,
                    "expected": {
                        "status": job.status,
                        "checkpoint_sequence": job.checkpoint_sequence,
                        "event_sequence": task.event_sequence,
                    },
                }
            )

    async def approvals(self, task_id: str, *, limit: int, offset: int) -> WorkbenchApprovalPage:
        async with self.sessions() as session:
            await self.root(session, task_id)
            job = await session.get(WorkflowJobRow, task_id)
            version = await session.scalar(
                select(func.max(TaskPlanRow.version)).where(TaskPlanRow.task_id == task_id)
            )
            condition = WorkflowApprovalRow.task_id == task_id
            total = await session.scalar(
                select(func.count()).select_from(WorkflowApprovalRow).where(condition)
            )
            rows = (
                await session.scalars(
                    select(WorkflowApprovalRow)
                    .where(condition)
                    .order_by(WorkflowApprovalRow.created_at.desc(), WorkflowApprovalRow.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            items = []
            for row in rows:
                expired = utc(row.expires_at) <= datetime.now(UTC)
                status = (
                    "expired" if expired and row.status in {"pending", "approved"} else row.status
                )
                actions = []
                if (
                    job
                    and job.status == "awaiting_approval"
                    and row.payload["plan_version"] == version
                ):
                    if row.status == "pending" and not expired:
                        actions.append("approve")
                    if row.status in {"pending", "expired"}:
                        actions.append("reject")
                    if row.status in {"pending", "expired", "rejected"}:
                        actions.append("modify")
                # Explicit projection: no arbitrary operation payload, code, paths,
                # source hashes, Memory context, worker identity or credentials.
                items.append(
                    WorkbenchApproval.model_validate(
                        {
                            "id": row.id,
                            "operation_sha256": row.operation_sha256,
                            "status": status,
                            "plan_version": row.payload["plan_version"],
                            "step_id": row.payload["step_id"],
                            "specification": row.payload["specification"],
                            "budget_limits": row.payload["budget_limits"],
                            "cost_units": row.payload["cost_units"],
                            "reason": row.reason[:2000],
                            "expires_at": utc(row.expires_at),
                            "actions": actions,
                        }
                    )
                )
            return WorkbenchApprovalPage(
                task_id=task_id, items=items, total=int(total or 0), limit=limit, offset=offset
            )

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
            project = await session.get(ProjectRow, project_id)
            if project is None or project.deleted_at is not None:
                raise AppError(
                    status_code=404, code="project_not_found", message="Project was not found"
                )
            conditions = [
                AgentTaskRow.project_id == project_id,
                AgentTaskRow.parent_task_id.is_(None),
                ~self.deleted(AgentTaskRow.id),
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
                results = payload.get("results", {})
                active_result = (
                    results.get(job.active.get("step_id"), {}) if isinstance(results, dict) else {}
                )
                cause = active_result.get("error_code") if isinstance(active_result, dict) else None
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
                        "cause_error_code": cause
                        if isinstance(cause, str) and len(cause) <= 64
                        else None,
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
