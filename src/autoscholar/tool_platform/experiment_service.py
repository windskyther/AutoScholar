"""Durable async execution with conservative crash recovery, not automatic replay."""

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from autoscholar.agent.database_models import ExperimentRow
from autoscholar.coding.sandbox import SandboxExecutor, SandboxRunRequest
from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.experiment.service import ExperimentService
from autoscholar.orchestration.approvals import cost_units
from autoscholar.orchestration.checkpoints import Snapshot, digest
from autoscholar.orchestration.durable_models import (
    WorkflowApprovalRow,
    WorkflowCheckpointRow,
    WorkflowJobRow,
)
from autoscholar.orchestration.durable_repository import utc
from autoscholar.tool_platform.artifact_spool import ArtifactSpool
from autoscholar.tool_platform.experiment_contracts import REPLY
from autoscholar.tool_platform.gateway import validate
from autoscholar.tool_platform.operation_models import (
    CoreToolCallRow,
    ExperimentExecutionRow,
    ToolOperationRow,
)
from autoscholar.tool_platform.operations import OperationDenied, OperationStore


def source_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def reply(
    status: str, result: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "result": result,
        "error_code": error,
        "uncertain": status == "uncertain",
    }


class ExperimentExecutionService:
    def __init__(
        self,
        operations: OperationStore,
        workspace: WorkspaceManager,
        sandbox: SandboxExecutor,
        spool: ArtifactSpool,
        *,
        approval_threshold: int = 20000,
        max_active: int = 4,
    ) -> None:
        self.operations, self.workspace, self.sandbox, self.spool = (
            operations,
            workspace,
            sandbox,
            spool,
        )
        self.owner = str(uuid4())
        self.approval_threshold, self.max_active = approval_threshold, max_active
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.closed = False

    async def authorize_run(
        self,
        session: AsyncSession,
        context: dict[str, Any],
        args: dict[str, Any],
        files: dict[str, str],
    ) -> None:
        task = await self.operations.authorize(session, context)
        call = await session.get(CoreToolCallRow, context["operation_id"])
        if (
            call is None
            or call.service != "experiment"
            or call.tool != "execute"
            or call.task_id != task.id
            or call.arguments_sha256 != context["arguments_sha256"]
            or call.authority_sha256 != self.operations.authority(context)
            or call.status != "prepared"
        ):
            raise OperationDenied("core_execution_required")
        if source_hash(files) != args["source_sha256"]:
            raise OperationDenied("experiment_source_changed")
        training = args["action"] == "run_python" and bool(args["collect_artifacts"])
        if (
            task.mode == "experiment"
            and args["action"] == "run_python"
            and (
                not training
                or args["path"] != "train.py"
                or args["args"]
                or set(args["collect_artifacts"]) != set(ExperimentService.required_paths)
            )
        ):
            raise OperationDenied("experiment_action_denied")
        spec = None
        if training:
            if task.mode != "experiment":
                raise OperationDenied("experiment_task_required")
            try:
                spec = ExperimentSpecification.model_validate_json(files["experiment_config.json"])
            except (KeyError, ValueError) as exc:
                raise OperationDenied("experiment_spec_invalid") from exc
            experiment = await session.scalar(
                select(ExperimentRow)
                .where(ExperimentRow.task_id == task.id, ExperimentRow.status == "running")
                .order_by(ExperimentRow.created_at.desc())
                .limit(1)
            )
            if (
                experiment is None
                or experiment.specification != spec.model_dump()
                or experiment.source_sha256 != args["source_sha256"]
                or experiment.dataset_sha256 != args["dataset_sha256"]
                or experiment.dataset_id != "mnist"
            ):
                raise OperationDenied("experiment_metadata_mismatch")
        if task.parent_task_id is None:
            return
        job = await session.get(WorkflowJobRow, task.parent_task_id)
        assert job is not None
        if (
            job.pending_calls.get(context["operation_id"]) != "mcp:execute"
            or job.active.get("stage") != "executor"
        ):
            raise OperationDenied("workflow_execution_not_active")
        checkpoint = await session.scalar(
            select(WorkflowCheckpointRow).where(
                WorkflowCheckpointRow.task_id == job.task_id,
                WorkflowCheckpointRow.sequence == job.checkpoint_sequence,
            )
        )
        if checkpoint is None or digest(checkpoint.payload) != checkpoint.sha256:
            raise OperationDenied("workflow_checkpoint_invalid")
        snapshot = Snapshot.model_validate(checkpoint.payload)
        if snapshot.plan is None or snapshot.version != job.active.get("version"):
            raise OperationDenied("workflow_plan_changed")
        for resource in ("sandbox_runs", "tool_calls", "training_runs"):
            if job.usage.get(resource, 0) > getattr(snapshot.limits, resource):
                raise OperationDenied("experiment_budget_exceeded")
        step = next((s for s in snapshot.plan.steps if s.id == job.active.get("step_id")), None)
        if step is None or step.type != task.mode:
            raise OperationDenied("workflow_step_mismatch")
        if step.type == "coding" and args["action"] not in {"static_check", "run_pytest"}:
            raise OperationDenied("training_in_coding_denied")
        if step.type == "experiment":
            if args["action"] not in {"static_check", "run_pytest", "run_python"} or (
                args["action"] == "run_python"
                and (not training or args["path"] != "train.py" or args["args"])
            ):
                raise OperationDenied("experiment_action_denied")
            upstream = next(
                (snapshot.sources[key] for key in step.dependencies if key in snapshot.sources),
                None,
            )
            expected = dict(upstream or {})
            expected["experiment_config.json"] = json.dumps(
                snapshot.specification.model_dump(), indent=2
            )
            if files != expected:
                raise OperationDenied("experiment_source_changed")
            if training and (
                spec != snapshot.specification
                or job.usage.get("training_runs", 0) < 1
                or job.usage.get("sandbox_runs", 0) < 1
                or job.usage.get("tool_calls", 0) < 1
            ):
                raise OperationDenied("experiment_budget_not_reserved")
            if cost_units(snapshot.specification) >= min(
                snapshot.approval_threshold, self.approval_threshold
            ):
                payload = {
                    "task_id": job.task_id,
                    "plan_version": snapshot.version,
                    "step_id": step.id,
                    "operation": "experiment",
                    "risk_level": 3,
                    "specification": snapshot.specification.model_dump(),
                    "source_sha256": source_hash(upstream or {}),
                    "budget_limits": snapshot.limits.model_dump(),
                    "cost_units": cost_units(snapshot.specification),
                }
                operation_hash = digest(payload)
                approval = await session.scalar(
                    select(WorkflowApprovalRow).where(
                        WorkflowApprovalRow.task_id == job.task_id,
                        WorkflowApprovalRow.operation_sha256 == operation_hash,
                    )
                )
                if (
                    job.active.get("operation_sha256") != operation_hash
                    or approval is None
                    or approval.status != "consumed"
                    or approval.payload != payload
                    or utc(approval.expires_at) <= datetime.now(UTC)
                ):
                    raise OperationDenied("experiment_approval_required")
            if training:
                # One training dispatch per immutable workflow unit, even with a new operation ID.
                rows = (
                    await session.scalars(
                        select(ExperimentExecutionRow)
                        .join(ToolOperationRow)
                        .where(ToolOperationRow.task_id == task.id)
                    )
                ).all()
                if any(
                    row.id != context["operation_id"] and row.request["action"] == "run_python"
                    for row in rows
                ):
                    raise OperationDenied("training_already_dispatched")

    async def submit(self, context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            return reply("denied", error="experiment_service_stopping")
        try:
            async with self.operations.sessions() as session:
                await self.operations.authorize(session, context)
                prior = await session.get(ToolOperationRow, context["operation_id"])
                if prior is not None:
                    if (
                        self.operations.receipt(prior, context).get("error_code")
                        == "operation_identity_conflict"
                    ):
                        return reply("denied", error="operation_identity_conflict")
                    return await self.state(session, prior)
                files = self.workspace.source_snapshot(context["scope"]["task_id"])
                await self.authorize_run(session, context, args, files)
                request = SandboxRunRequest(
                    task_id=context["scope"]["task_id"],
                    files=files,
                    **{
                        k: v
                        for k, v in args.items()
                        if k not in {"source_sha256", "dataset_sha256"}
                    },
                )
                if args["dataset_sha256"] is not None:
                    health = await self.sandbox.health()
                    if (
                        health.status != "ok"
                        or health.dataset_id != "mnist"
                        or health.dataset_sha256 != args["dataset_sha256"]
                        or not health.mnist_dataset
                    ):
                        raise OperationDenied("experiment_dataset_changed")
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    await session.execute(text("SELECT pg_advisory_xact_lock(80940001)"))
                count = await session.scalar(
                    select(func.count())
                    .select_from(ExperimentExecutionRow)
                    .where(
                        ExperimentExecutionRow.status.in_(["queued", "running"]),
                        ExperimentExecutionRow.heartbeat > time.time() - 15,
                    )
                )
                if count is not None and count >= self.max_active:
                    raise OperationDenied("experiment_capacity_reached")
                deadline = time.time() + request.timeout_seconds + 10
                # Invocation deadline protects submission; execution gets a separate fixed limit.
                run_context = {**context, "deadline": deadline}
                session.add(
                    ToolOperationRow(
                        id=context["operation_id"],
                        task_id=request.task_id,
                        service="experiment",
                        tool="execute",
                        arguments_sha256=context["arguments_sha256"],
                        authority_sha256=self.operations.authority(context),
                        status="running",
                        result=None,
                    )
                )
                await session.flush()
                session.add(
                    ExperimentExecutionRow(
                        id=context["operation_id"],
                        context=run_context,
                        request=args,
                        owner=self.owner,
                        status="queued",
                        heartbeat=time.time(),
                        deadline=deadline,
                    )
                )
                await session.commit()
            operation_id = context["operation_id"]
            self.tasks[operation_id] = asyncio.create_task(self.execute(run_context, args, request))
            self.tasks[operation_id].add_done_callback(lambda _: self.tasks.pop(operation_id, None))
            return reply("queued")
        except OperationDenied as exc:
            return reply("denied", error=str(exc))
        except WorkspaceError as exc:
            return reply("denied", error=exc.code)
        except ValueError:
            return reply("denied", error="experiment_request_invalid")

    async def guard(
        self, context: dict[str, Any], args: dict[str, Any], request: SandboxRunRequest
    ) -> None:
        async with asyncio.timeout(2), self.operations.sessions() as session:
            await self.authorize_run(session, context, args, request.files)
            row = await session.get(ExperimentExecutionRow, context["operation_id"])
            if row is None or row.owner != self.owner or row.status not in {"queued", "running"}:
                raise OperationDenied("experiment_execution_cancelled")
            row.status, row.heartbeat = "running", time.time()
            await session.commit()

    async def execute(
        self, context: dict[str, Any], args: dict[str, Any], request: SandboxRunRequest
    ) -> None:
        operation_id = context["operation_id"]
        run: asyncio.Task[Any] | None = None
        watcher: asyncio.Task[None] | None = None

        async def watch() -> None:
            while True:
                await asyncio.sleep(0.5)
                await self.guard(context, args, request)

        try:
            await self.guard(context, args, request)
            run = asyncio.create_task(self.sandbox.run(request))
            watcher = asyncio.create_task(watch())
            async with asyncio.timeout(max(0, context["deadline"] - time.time())):
                done, _ = await asyncio.wait({run, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    await watcher
                    raise OperationDenied("experiment_watchdog_stopped")
                result = await run

                async def publish() -> dict[str, Any]:
                    manifest = self.spool.save(operation_id, result, request.collect_artifacts)
                    value = reply("completed", manifest)
                    validate(REPLY, value, "experiment_result_invalid")
                    return value

                await self.operations.finish(context, publish)
            await self.mark(operation_id, "completed")
        except BaseException:
            # Loss of lease/DB/process/result integrity never authorizes a second sandbox run.
            # Cancellation waits for SandboxClient disconnect before becoming uncertain.
            if run is not None and not run.done():
                run.cancel()
                with suppress(BaseException):
                    await run
            with suppress(Exception):
                await self.mark(operation_id, "uncertain")
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(BaseException):
                    await watcher

    async def mark(self, operation_id: str, status: str) -> None:
        async with asyncio.timeout(3), self.operations.sessions() as session:
            row = await session.get(ExperimentExecutionRow, operation_id)
            if row is not None and row.owner == self.owner:
                row.status, row.heartbeat = status, time.time()
                await session.commit()

    async def state(self, session: AsyncSession, operation: ToolOperationRow) -> dict[str, Any]:
        if operation.status == "completed" and operation.result is not None:
            return operation.result
        row = await session.get(ExperimentExecutionRow, operation.id)
        if row is not None and row.status == "completed":
            # A READ COMMITTED query may straddle publication. Re-read its receipt,
            # never manufacture a completed status with a null result.
            await session.refresh(operation)
            if operation.status == "completed" and operation.result is not None:
                return operation.result
            return reply("uncertain", error="experiment_receipt_missing")
        if (
            row is None
            or row.status in {"uncertain", "cancelled"}
            or row.heartbeat < time.time() - 15
            or row.deadline <= time.time()
        ):
            return reply("uncertain", error="experiment_result_uncertain")
        return reply(row.status)

    async def control(
        self, operation_id: str, context: dict[str, Any], *, cancel: bool = False
    ) -> dict[str, Any]:
        try:
            async with self.operations.sessions() as session:
                task = await self.operations.authorize(session, context, read_only=True)
                operation = await session.get(ToolOperationRow, operation_id)
                if (
                    operation is None
                    or operation.task_id != task.id
                    or operation.service != "experiment"
                ):
                    return reply("missing")
                result = await self.state(session, operation)
                if cancel and operation.status != "completed":
                    row = await session.get(ExperimentExecutionRow, operation_id)
                    if row is not None:
                        row.status = "cancelled"
                        await session.commit()
            if cancel and operation_id in self.tasks:
                self.tasks[operation_id].cancel()
                await self.tasks[operation_id]
                return reply("uncertain", error="experiment_cancelled")
            return result
        except OperationDenied as exc:
            return reply("denied", error=str(exc))

    async def close(self) -> None:
        self.closed = True
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.sandbox.close()
