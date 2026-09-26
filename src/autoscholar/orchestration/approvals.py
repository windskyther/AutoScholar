"""Server-owned approval rules and decisions bound to immutable operation parameters."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from autoscholar.agent.database_models import TaskPlanRow
from autoscholar.core.errors import AppError
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.orchestration.checkpoints import Snapshot, digest
from autoscholar.orchestration.durable_models import WorkflowApprovalRow
from autoscholar.orchestration.durable_repository import DurableRepository, conflict, utc
from autoscholar.orchestration.models import PlanStep, TaskPlan
from autoscholar.orchestration.service import Run, source_digest


def cost_units(specification: ExperimentSpecification) -> int:
    return specification.epochs * specification.train_samples * len(specification.models)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "modify"]
    operation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=2000)
    specification: ExperimentSpecification | None = None

    @model_validator(mode="after")
    def modification(self) -> "ApprovalDecision":
        if (self.action == "modify") != (self.specification is not None):
            raise ValueError("Only modify requires a complete supported experiment specification")
        return self


class ApprovalService:
    def __init__(self, repository: DurableRepository) -> None:
        self.repository = repository

    @staticmethod
    def operation(run: Run, step: PlanStep) -> dict[str, Any]:
        source_key = next(key for key in step.dependencies if key in run.sources)
        return {
            "task_id": run.task_id,
            "plan_version": run.version,
            "step_id": step.id,
            "operation": "experiment",
            "risk_level": 3,
            "specification": run.specification.model_dump(),
            "source_sha256": source_digest(run.sources[source_key]),
            "budget_limits": run.budget.limits.model_dump(),
            "cost_units": cost_units(run.specification),
        }

    async def gate(self, run: Run, step: PlanStep, owner: str, generation: int) -> bool:
        payload = self.operation(run, step)
        operation_hash = digest(payload)
        async with self.repository.sessions() as session:
            job = await self.repository.locked(session, run.task_id)
            self.repository.fence(job, owner, generation)
            row = await session.scalar(
                select(WorkflowApprovalRow).where(
                    WorkflowApprovalRow.task_id == run.task_id,
                    WorkflowApprovalRow.operation_sha256 == operation_hash,
                )
            )
            if row is None:
                row = WorkflowApprovalRow(
                    id=str(uuid4()),
                    task_id=run.task_id,
                    operation_sha256=operation_hash,
                    payload=payload,
                    status="pending",
                    reason="Experiment exceeds approval threshold",
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
                session.add(row)
                self.repository.event(
                    session,
                    run.task_id,
                    "approval_requested",
                    approval_id=row.id,
                    operation_sha256=operation_hash,
                )
            permitted = row.status == "approved" and utc(row.expires_at) > datetime.now(UTC)
            if row.status in {"pending", "approved"} and utc(row.expires_at) <= datetime.now(UTC):
                row.status = "expired"
            await session.commit()
            return permitted

    async def list(self, task_id: str) -> list[dict[str, Any]]:
        async with self.repository.sessions() as session:
            await self.repository.locked(session, task_id)
            rows = (
                await session.scalars(
                    select(WorkflowApprovalRow)
                    .where(
                        WorkflowApprovalRow.task_id == task_id,
                    )
                    .order_by(WorkflowApprovalRow.created_at, WorkflowApprovalRow.id)
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "operation_sha256": row.operation_sha256,
                    "status": (
                        "expired"
                        if row.status in {"pending", "approved"}
                        and utc(row.expires_at) <= datetime.now(UTC)
                        else row.status
                    ),
                    "operation": row.payload,
                    "reason": row.reason,
                    "expires_at": row.expires_at.isoformat(),
                }
                for row in rows
            ]

    async def decide(self, task_id: str, approval_id: str, decision: ApprovalDecision) -> str:
        snapshot, prior = await self.repository.snapshot(task_id)
        async with self.repository.sessions() as session:
            job = await self.repository.locked(session, task_id)
            row = await session.get(WorkflowApprovalRow, approval_id)
            if row is None or row.task_id != task_id:
                raise AppError(
                    status_code=404, code="approval_not_found", message="Approval not found"
                )
            if (
                row.operation_sha256 != decision.operation_sha256
                or digest(row.payload) != (decision.operation_sha256)
                or row.payload["plan_version"] != snapshot.version
            ):
                raise conflict("approval_stale", "Approval parameters or plan have changed")
            # Repeated identical decisions are harmless. Reversals require a new plan.
            target = {"approve": "approved", "reject": "rejected", "modify": "superseded"}[
                decision.action
            ]
            if row.status == target and decision.action != "modify":
                return job.status
            if job.status != "awaiting_approval" or job.checkpoint_sequence != (
                prior.checkpoint_sequence
            ):
                raise conflict("workflow_state_conflict", "Task is not waiting for this approval")
            if decision.action == "approve" and (
                row.status != "pending" or utc(row.expires_at) <= datetime.now(UTC)
            ):
                raise conflict(
                    "approval_expired_or_decided", "Approval expired or was already decided"
                )
            if decision.action == "reject" and row.status not in {"pending", "expired"}:
                raise conflict("approval_already_decided", "Approval was already decided")
            if decision.action == "modify":
                assert decision.specification is not None and snapshot.plan is not None
                if row.status not in {"pending", "rejected", "expired"}:
                    raise conflict("approval_already_decided", "Approval was already decided")
                snapshot = self.modified(snapshot, decision.specification)
                assert snapshot.plan is not None
                session.add(
                    TaskPlanRow(
                        id=str(uuid4()),
                        task_id=task_id,
                        version=snapshot.version,
                        payload=snapshot.plan.model_dump(),
                        reason="Human changed experiment parameters",
                    )
                )
                self.repository.checkpoint(session, job, snapshot)
            row.status = target
            row.reason = decision.reason
            row.decided_at = datetime.now(UTC)
            status = "paused" if decision.action in {"approve", "modify"} else "awaiting_approval"
            await self.repository.status(session, job, status)
            self.repository.event(
                session,
                task_id,
                "approval_decided",
                approval_id=approval_id,
                decision=decision.action,
                reason=decision.reason,
                operation_sha256=decision.operation_sha256,
            )
            await session.commit()
            return status

    @staticmethod
    def modified(snapshot: Snapshot, specification: ExperimentSpecification) -> Snapshot:
        assert snapshot.plan is not None
        snapshot = Snapshot.model_validate(snapshot.model_dump())
        assert snapshot.plan is not None
        affected = {
            step.id for step in snapshot.plan.steps if step.type in {"coding", "experiment"}
        }
        while True:
            dependent = {
                step.id for step in snapshot.plan.steps if set(step.dependencies) & affected
            }
            if dependent <= affected:
                break
            affected |= dependent
        for key in affected:
            snapshot.results.pop(key, None)
            source = snapshot.sources.pop(key, None)
            if source is not None:
                snapshot.previous_sources[key] = source
        snapshot.plan = TaskPlan(
            goal=snapshot.plan.goal,
            steps=[
                step.model_copy(update={"specification": None})
                if step.type == "experiment"
                else step
                for step in snapshot.plan.steps
            ],
        )
        snapshot.specification = specification
        snapshot.version += 1
        snapshot.stage = "executor"
        snapshot.review, snapshot.answer = None, None
        return snapshot
