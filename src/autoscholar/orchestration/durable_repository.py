"""Transactional queue, fenced leases, immutable checkpoints and operation accounting."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.core.errors import AppError
from autoscholar.orchestration.checkpoints import Snapshot, digest
from autoscholar.orchestration.durable_models import (
    WorkflowApprovalRow,
    WorkflowCheckpointRow,
    WorkflowEventRow,
    WorkflowJobRow,
)
from autoscholar.rag.database_models import ProjectRow

TERMINAL = {"succeeded", "failed", "budget_exceeded", "cancelled"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def conflict(code: str, message: str) -> AppError:
    return AppError(status_code=409, code=code, message=message)


class LeaseLost(RuntimeError):
    pass


class DurableRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def event(session: AsyncSession, task_id: str, kind: str, **payload: Any) -> None:
        session.add(
            WorkflowEventRow(
                id=str(uuid4()),
                task_id=task_id,
                kind=kind,
                payload=payload,
            )
        )

    @staticmethod
    async def locked(session: AsyncSession, task_id: str) -> WorkflowJobRow:
        row = await session.scalar(
            select(WorkflowJobRow).where(WorkflowJobRow.task_id == task_id).with_for_update()
        )
        if row is None:
            raise AppError(status_code=404, code="workflow_not_found", message="Workflow not found")
        return row

    @staticmethod
    def fence(row: WorkflowJobRow, owner: str, generation: int) -> None:
        if (
            row.owner != owner
            or row.generation != generation
            or row.lease_until is None
            or utc(row.lease_until) <= datetime.now(UTC)
        ):
            raise LeaseLost("Workflow lease expired or belongs to another worker")

    @staticmethod
    async def status(session: AsyncSession, row: WorkflowJobRow, status: str) -> None:
        row.status = status
        task = await session.get(AgentTaskRow, row.task_id)
        assert task is not None
        task.status = status
        task.metrics = dict(row.usage) | {"wall_seconds": int(row.active_seconds)}
        task.error_code = row.error_code
        task.error_message = (
            "Execution needs inspection; no automatic replay of uncertain operations"
            if status == "recovery_required"
            else None
        )

    @staticmethod
    def checkpoint(session: AsyncSession, row: WorkflowJobRow, snapshot: Snapshot) -> None:
        payload = snapshot.model_dump(mode="json")
        row.checkpoint_sequence += 1
        session.add(
            WorkflowCheckpointRow(
                id=str(uuid4()),
                task_id=row.task_id,
                sequence=row.checkpoint_sequence,
                payload=payload,
                sha256=digest(payload),
            )
        )

    async def enqueue(self, snapshot: Snapshot, key: str, request_hash: str) -> tuple[str, bool]:
        async with self.sessions() as session:
            existing = await session.scalar(
                select(WorkflowJobRow).where(WorkflowJobRow.idempotency_key == key)
            )
            if existing is not None:
                if existing.request_sha256 != request_hash:
                    raise conflict("idempotency_conflict", "Key was used with a different request")
                return existing.task_id, False
            if snapshot.project_id and await session.get(ProjectRow, snapshot.project_id) is None:
                raise AppError(
                    status_code=404, code="project_not_found", message="Project not found"
                )
            session.add(
                AgentTaskRow(
                    id=snapshot.task_id,
                    objective=snapshot.objective,
                    project_id=snapshot.project_id,
                    status="queued",
                    mode="autonomous",
                    plan=[],
                    metrics={},
                    research_sources=snapshot.research_sources,
                )
            )
            await session.flush()
            row = WorkflowJobRow(
                task_id=snapshot.task_id,
                idempotency_key=key,
                request_sha256=request_hash,
                status="queued",
                checkpoint_sequence=0,
                generation=0,
                active={},
                pending_calls={},
                usage={},
                active_seconds=0,
            )
            session.add(row)
            self.checkpoint(session, row, snapshot)
            self.event(session, row.task_id, "submitted")
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                existing = await session.scalar(
                    select(WorkflowJobRow).where(WorkflowJobRow.idempotency_key == key)
                )
                if existing is None:
                    raise
                if existing.request_sha256 != request_hash:
                    raise conflict(
                        "idempotency_conflict", "Key was used with a different request"
                    ) from exc
                return existing.task_id, False
        return snapshot.task_id, True

    async def snapshot(self, task_id: str) -> tuple[Snapshot, WorkflowJobRow]:
        async with self.sessions() as session:
            row = await session.get(WorkflowJobRow, task_id)
            if row is None:
                raise AppError(
                    status_code=404, code="workflow_not_found", message="Workflow not found"
                )
            checkpoint = await session.scalar(
                select(WorkflowCheckpointRow).where(
                    WorkflowCheckpointRow.task_id == task_id,
                    WorkflowCheckpointRow.sequence == row.checkpoint_sequence,
                )
            )
            if checkpoint is None or checkpoint.sha256 != digest(checkpoint.payload):
                raise conflict("checkpoint_integrity_failed", "Checkpoint integrity check failed")
            snapshot = Snapshot.model_validate(checkpoint.payload)
            if snapshot.task_id != task_id:
                raise conflict("checkpoint_integrity_failed", "Checkpoint task mismatch")
            session.expunge(row)
            return snapshot, row

    async def claim(
        self,
        owner: str,
        lease_seconds: int,
        task_id: str | None = None,
    ) -> tuple[str, int] | None:
        now = datetime.now(UTC)
        async with self.sessions() as session:
            query = (
                select(WorkflowJobRow)
                .where(
                    or_(
                        WorkflowJobRow.status == "queued",
                        WorkflowJobRow.status.in_(
                            ["running", "pause_requested", "cancel_requested"]
                        )
                        & (WorkflowJobRow.lease_until < now),
                    )
                )
                .order_by(WorkflowJobRow.created_at, WorkflowJobRow.task_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if task_id is not None:
                query = query.where(WorkflowJobRow.task_id == task_id)
            row = await session.scalar(query)
            if row is None:
                return None
            prior = row.status
            row.generation += 1
            row.owner = owner
            row.lease_until = now + timedelta(seconds=lease_seconds)
            # An expired in-flight unit must be reconciled by the worker, never replayed blindly.
            if prior == "queued":
                await self.status(session, row, "running")
            self.event(session, row.task_id, "claimed", generation=row.generation, previous=prior)
            await session.commit()
            return row.task_id, row.generation

    async def heartbeat(self, task_id: str, owner: str, generation: int, seconds: int) -> str:
        async with self.sessions() as session:
            row = await self.locked(session, task_id)
            self.fence(row, owner, generation)
            row.lease_until = datetime.now(UTC) + timedelta(seconds=seconds)
            await session.commit()
            return row.status

    async def quarantine(self, task_id: str, owner: str, generation: int) -> None:
        async with self.sessions() as session:
            row = await self.locked(session, task_id)
            self.fence(row, owner, generation)
            row.error_code = "checkpoint_integrity_failed"
            row.owner, row.lease_until = None, None
            await self.status(session, row, "recovery_required")
            self.event(session, task_id, "checkpoint_rejected")
            await session.commit()

    async def begin(
        self,
        task_id: str,
        owner: str,
        generation: int,
        active: dict[str, Any],
    ) -> str:
        async with self.sessions() as session:
            row = await self.locked(session, task_id)
            self.fence(row, owner, generation)
            if row.active or row.pending_calls:
                raise conflict("operation_uncertain", "An earlier operation needs reconciliation")
            if row.status == "running":
                if operation_hash := active.get("operation_sha256"):
                    approval = await session.scalar(
                        select(WorkflowApprovalRow).where(
                            WorkflowApprovalRow.task_id == task_id,
                            WorkflowApprovalRow.operation_sha256 == operation_hash,
                        )
                    )
                    if (
                        approval is None
                        or approval.status != "approved"
                        or utc(approval.expires_at) <= datetime.now(UTC)
                    ):
                        raise conflict("approval_required", "A current approval is required")
                    approval.status = "consumed"
                    self.event(session, task_id, "approval_consumed", approval_id=approval.id)
                row.active = active
                self.event(session, task_id, "unit_started", **active)
            await session.commit()
            return row.status

    async def journal(
        self,
        task_id: str,
        owner: str,
        generation: int,
        call_id: str,
        kind: str,
        starting: bool,
        usage: dict[str, int],
        elapsed: float,
    ) -> None:
        async with self.sessions() as session:
            row = await self.locked(session, task_id)
            self.fence(row, owner, generation)
            pending = dict(row.pending_calls)
            if kind == "budget":
                pass
            elif starting:
                if pending:
                    raise conflict("operation_uncertain", "Previous external result is uncertain")
                if row.status == "cancel_requested":
                    raise conflict("workflow_cancel_requested", "Cancellation was requested")
                pending[call_id] = kind
            else:
                if pending.pop(call_id, None) != kind:
                    raise LeaseLost("Operation journal mismatch")
            row.pending_calls = pending
            row.usage = {key: max(value, row.usage.get(key, 0)) for key, value in usage.items()}
            row.active_seconds = max(row.active_seconds, elapsed)
            self.event(
                session,
                task_id,
                "budget_saved"
                if kind == "budget"
                else ("call_started" if starting else "call_finished"),
                call_id=call_id,
                kind_name=kind,
            )
            await session.commit()

    async def save(
        self,
        task_id: str,
        owner: str,
        generation: int,
        snapshot: Snapshot,
        usage: dict[str, int],
        elapsed: float,
        *,
        status: str = "queued",
        error_code: str | None = None,
    ) -> None:
        async with self.sessions() as session:
            row = await self.locked(session, task_id)
            self.fence(row, owner, generation)
            if row.status == "cancel_requested":
                status = "cancelled"
            elif row.pending_calls and status not in {"cancelled", "budget_exceeded"}:
                status, error_code = "recovery_required", "external_result_uncertain"
            elif row.status == "pause_requested" and status == "queued":
                status = "paused"
            row.usage = {key: max(value, row.usage.get(key, 0)) for key, value in usage.items()}
            row.active_seconds = max(row.active_seconds, elapsed)
            row.error_code = error_code
            self.checkpoint(session, row, snapshot)
            if status != "recovery_required":
                row.active = {}
            row.owner, row.lease_until = None, None
            await self.status(session, row, status)
            task = await session.get(AgentTaskRow, task_id)
            assert task is not None
            task.plan = [step.description for step in snapshot.plan.steps] if snapshot.plan else []
            task.answer = snapshot.answer if status == "succeeded" else None
            children = (
                await session.scalars(
                    select(AgentTaskRow).where(
                        AgentTaskRow.parent_task_id == task_id,
                    )
                )
            ).all()
            task.metrics = dict(task.metrics) | {
                key: sum(child.metrics.get(key, 0) for child in children)
                for key in (
                    "artifact_count",
                    "artifact_bytes",
                    "files_written",
                    "experiment_duration_ms",
                    "experiments_started",
                    "experiments_succeeded",
                )
            }
            self.event(
                session,
                task_id,
                "checkpoint_saved",
                sequence=row.checkpoint_sequence,
                stage=snapshot.stage,
                status=status,
            )
            await session.commit()

    async def control(self, task_id: str, action: str) -> str:
        async with self.sessions() as session:
            row = await self.locked(session, task_id)
            status = row.status
            if action == "pause":
                if status in {"queued", "paused"}:
                    status = "paused"
                elif status in {"running", "pause_requested"}:
                    status = "pause_requested"
                else:
                    raise conflict("workflow_state_conflict", "Task cannot be paused in this state")
            elif action == "resume":
                if status == "paused":
                    status = "queued"
                elif status not in {"queued", "running"}:
                    raise conflict(
                        "workflow_state_conflict", "Resolve approval/recovery before resume"
                    )
            elif action == "cancel":
                if status in {"running", "pause_requested", "cancel_requested"}:
                    status = "cancel_requested"
                elif status not in TERMINAL:
                    status = "cancelled"
            else:
                raise ValueError("Unknown workflow action")
            await self.status(session, row, status)
            self.event(session, task_id, action, status=status)
            await session.commit()
            return status

    async def history(self, task_id: str, kind: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            await self.locked(session, task_id)
            if kind == "checkpoints":
                checkpoints = (
                    await session.scalars(
                        select(WorkflowCheckpointRow)
                        .where(
                            WorkflowCheckpointRow.task_id == task_id,
                        )
                        .order_by(WorkflowCheckpointRow.sequence.desc())
                        .limit(limit)
                    )
                ).all()
                return [
                    {
                        "sequence": row.sequence,
                        "sha256": row.sha256,
                        "stage": row.payload["stage"],
                        "plan_version": row.payload["version"],
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in checkpoints
                ]
            events = (
                await session.scalars(
                    select(WorkflowEventRow)
                    .where(
                        WorkflowEventRow.task_id == task_id,
                    )
                    .order_by(WorkflowEventRow.created_at.desc(), WorkflowEventRow.id.desc())
                    .limit(limit)
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat(),
                }
                for row in events
            ]
