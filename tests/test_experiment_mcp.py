import asyncio
import base64
import hashlib
import json
import socket
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import uvicorn

from autoscholar.agent.database_models import AgentTaskRow, ExperimentRow
from autoscholar.coding.sandbox import (
    SandboxArtifact,
    SandboxError,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.core.journal import current_journal
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.experiment.service import ExperimentService
from autoscholar.orchestration.approvals import cost_units
from autoscholar.orchestration.checkpoints import Snapshot, digest
from autoscholar.orchestration.durable_models import (
    WorkflowApprovalRow,
    WorkflowCheckpointRow,
    WorkflowJobRow,
)
from autoscholar.orchestration.sandbox import BudgetedSandbox
from autoscholar.tool_platform.artifact_spool import ArtifactSpool
from autoscholar.tool_platform.context import ToolScope, tool_scope
from autoscholar.tool_platform.experiment_adapter import MCPExperimentSandbox
from autoscholar.tool_platform.experiment_contracts import EXPERIMENT_CONTRACTS, REPLY
from autoscholar.tool_platform.experiment_server import create_experiment_app
from autoscholar.tool_platform.experiment_service import ExperimentExecutionService, source_hash
from autoscholar.tool_platform.gateway import (
    ToolGateway,
    ToolGatewayError,
    argument_digest,
    canonical,
)
from autoscholar.tool_platform.operation_models import ExperimentExecutionRow, ToolOperationRow
from autoscholar.tool_platform.operations import OperationDenied, OperationStore
from autoscholar.tool_platform.registry import InvocationRegistry
from autoscholar.tool_platform.transport import MCPBackend
from tests.test_coding_agent import run_result
from tests.test_filesystem_mcp import context
from tests.test_workflow_foundation import plan

pytest_plugins = ["tests.test_filesystem_mcp"]
TOKEN = "offline-experiment-token-at-least-32-chars"


class WaitingSandbox:
    def __init__(self) -> None:
        self.requests: list[SandboxRunRequest] = []
        self.delay = 0.1
        self.cancelled = 0
        self.result = run_result()
        self.dataset_sha256 = "1" * 64

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.requests.append(request)
        try:
            await asyncio.sleep(self.delay)
            return self.result
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def health(self) -> SandboxHealth:
        return SandboxHealth(
            status="ok",
            engine=True,
            image=True,
            mnist_dataset=True,
            dataset_id="mnist",
            dataset_sha256=self.dataset_sha256,
        )

    async def close(self) -> None:
        pass


@pytest.fixture
async def execution(
    filesystem: Any, tmp_path: Path
) -> AsyncIterator[tuple[ExperimentExecutionService, WaitingSandbox, InvocationRegistry]]:
    operations, workspace = filesystem
    sandbox = WaitingSandbox()
    service = ExperimentExecutionService(
        OperationStore(operations.sessions, "experiment"),
        workspace,
        sandbox,
        ArtifactSpool(tmp_path / "spool"),
    )
    try:
        yield service, sandbox, InvocationRegistry(operations.sessions)
    finally:
        await service.close()


@pytest.fixture
async def experiment_http(execution: Any) -> AsyncIterator[ToolGateway]:
    service, _, _ = execution
    app = create_experiment_app(service, token=TOKEN, allowed_hosts=["127.0.0.1:*"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield ToolGateway(
            MCPBackend(f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", TOKEN),
            EXPERIMENT_CONTRACTS,
            timeout_seconds=1,
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        sock.close()


def arguments(service: ExperimentExecutionService) -> dict[str, Any]:
    return {
        "action": "static_check",
        "path": None,
        "args": [],
        "collect_artifacts": [],
        "timeout_seconds": 5,
        "source_sha256": source_hash(service.workspace.source_snapshot("file-task")),
        "dataset_sha256": None,
    }


async def prepare(
    service: ExperimentExecutionService, registry: InvocationRegistry, args: dict[str, Any]
) -> dict[str, Any]:
    ctx = context("execute", arguments_sha256=argument_digest(args))
    await registry.prepare("experiment", ctx, REPLY)
    return ctx


async def completed(service: ExperimentExecutionService, ctx: dict[str, Any]) -> dict[str, Any]:
    async with asyncio.timeout(8):
        while True:
            result = await service.control(ctx["operation_id"], ctx)
            if result["status"] not in {"queued", "running"}:
                return result
            await asyncio.sleep(0.02)


async def test_http_long_run_has_one_budget_and_journal_boundary(
    execution: Any, experiment_http: ToolGateway
) -> None:
    service, sandbox, registry = execution
    service.workspace.write_text("file-task", "a.py", "value = 1\n")
    sandbox.delay = 1.4  # Each HTTP call has a 1-second timeout; logical run may be longer.
    adapter = BudgetedSandbox(
        MCPExperimentSandbox(
            experiment_http, service.workspace, service.spool, registry, poll_seconds=0.05
        )
    )
    budget = Budget(BudgetLimits())
    events: list[tuple[str, str, bool]] = []

    async def journal(op: str, kind: str, starting: bool) -> None:
        events.append((op, kind, starting))

    token, hook = current_budget.set(budget), current_journal.set(journal)
    try:
        result = await adapter.run(
            SandboxRunRequest(
                task_id="file-task",
                action="static_check",
                files=service.workspace.source_snapshot("file-task"),
                timeout_seconds=5,
            )
        )
    finally:
        current_budget.reset(token)
        current_journal.reset(hook)
    assert result.status == "succeeded" and len(sandbox.requests) == 1
    assert budget.used == {"sandbox_runs": 1, "tool_calls": 1}
    assert (
        len(events) == 2
        and events[0][1:] == ("mcp:execute", True)
        and events[1] == (events[0][0], "mcp:execute", False)
    )
    items = await registry.inspect("file-task")
    assert len(items) == 1 and items[0]["receipt_status"] == "completed"


async def test_source_snapshot_larger_than_mcp_envelope_is_not_sent(
    execution: Any, experiment_http: ToolGateway
) -> None:
    service, sandbox, registry = execution
    service.workspace.max_file_bytes = 1048576
    for name in ("a.py", "b.py"):
        service.workspace.write_text("file-task", name, "#" + "x" * 600000)
    snapshot = service.workspace.source_snapshot("file-task")
    assert len(canonical(snapshot).encode()) > 1048576
    adapter = MCPExperimentSandbox(
        experiment_http, service.workspace, service.spool, registry, poll_seconds=0.05
    )
    result = await adapter.run(
        SandboxRunRequest(
            task_id="file-task", action="static_check", files=snapshot, timeout_seconds=5
        )
    )
    assert result.status == "succeeded" and sandbox.requests[0].files == snapshot


async def test_scope_source_core_identity_and_dataset_rejections(
    execution: Any, experiment_http: ToolGateway
) -> None:
    service, sandbox, registry = execution
    args = arguments(service)
    assert (await experiment_http.invoke("execute", args, journal=False))["status"] == "denied"
    with tool_scope(ToolScope("file-task")):
        assert (await experiment_http.invoke("execute", args, journal=False))[
            "error_code"
        ] == "core_execution_required"
    ctx = await prepare(service, registry, args)
    service.workspace.write_text("file-task", "changed.py", "one")
    assert (await service.submit(ctx, args))["error_code"] == "experiment_source_changed"
    args = dict(arguments(service), dataset_sha256="2" * 64)
    ctx = await prepare(service, registry, args)
    assert (await service.submit(ctx, args))["error_code"] == "experiment_dataset_changed"
    assert not sandbox.requests
    with (
        tool_scope(ToolScope("file-task")),
        pytest.raises(ToolGatewayError, match="tool_arguments_invalid"),
    ):
        await experiment_http.invoke("execute", {**args, "timeout_seconds": 601}, journal=False)


async def test_duplicate_completed_receipt_and_restart_never_reruns(execution: Any) -> None:
    service, sandbox, registry = execution
    args = arguments(service)
    ctx = await prepare(service, registry, args)
    assert (await service.submit(ctx, args))["status"] == "queued"
    assert (await service.submit(ctx, args))["status"] in {"queued", "running", "completed"}
    result = await completed(service, ctx)
    assert result["status"] == "completed" and len(sandbox.requests) == 1
    restarted = ExperimentExecutionService(
        service.operations, service.workspace, sandbox, service.spool
    )
    assert await restarted.submit(ctx, args) == result
    assert len(sandbox.requests) == 1
    changed = dict(ctx, arguments_sha256="f" * 64)
    assert (await restarted.submit(changed, args))["error_code"] == "operation_identity_conflict"
    foreign = dict(ctx, scope=ToolScope("missing-task").payload())
    assert (await restarted.control(ctx["operation_id"], foreign))["status"] == "denied"


async def test_cancel_and_lost_authority_stop_execution_without_replay(
    execution: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, sandbox, registry = execution
    sandbox.delay = 100
    args = arguments(service)
    ctx = await prepare(service, registry, args)
    await service.submit(ctx, args)
    async with asyncio.timeout(2):
        while not sandbox.requests:
            await asyncio.sleep(0.01)
    await service.control(ctx["operation_id"], ctx, cancel=True)
    assert sandbox.cancelled == 1
    assert (await service.submit(ctx, args))["uncertain"]
    ctx2 = await prepare(service, registry, args)
    await service.submit(ctx2, args)
    async with asyncio.timeout(2):
        while len(sandbox.requests) < 2:
            await asyncio.sleep(0.01)
    original = service.operations.authorize

    async def lost(*_: Any, **__: Any) -> Any:
        raise OperationDenied("workflow_claim_stale")

    monkeypatch.setattr(service.operations, "authorize", lost)
    async with asyncio.timeout(3):
        while sandbox.cancelled < 2:
            await asyncio.sleep(0.02)
    monkeypatch.setattr(service.operations, "authorize", original)
    assert (await service.control(ctx2["operation_id"], ctx2))["uncertain"]
    assert len(sandbox.requests) == 2


async def test_stale_process_record_is_uncertain_not_a_new_dispatch(execution: Any) -> None:
    service, sandbox, registry = execution
    args = arguments(service)
    ctx = await prepare(service, registry, args)
    async with service.operations.sessions() as session:
        session.add(
            ToolOperationRow(
                id=ctx["operation_id"],
                task_id="file-task",
                service="experiment",
                tool="execute",
                arguments_sha256=ctx["arguments_sha256"],
                authority_sha256=service.operations.authority(ctx),
                status="running",
                result=None,
            )
        )
        await session.flush()
        session.add(
            ExperimentExecutionRow(
                id=ctx["operation_id"],
                context=ctx,
                request=args,
                owner=str(uuid4()),
                status="running",
                heartbeat=time.time() - 20,
                deadline=time.time() + 100,
            )
        )
        await session.commit()
    assert (await service.submit(ctx, args))["uncertain"]
    assert not sandbox.requests


def test_large_artifact_uses_spool_and_detects_tampering(tmp_path: Path) -> None:
    spool = ArtifactSpool(tmp_path / "spool")
    data = b"binary-fixture\x00" * 150000
    artifact = SandboxArtifact(
        path="checkpoints/model.pt",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data_base64=base64.b64encode(data).decode(),
    )
    run = run_result().model_copy(update={"artifacts": [artifact]})
    op = str(uuid4())
    manifest = spool.save(op, run, [artifact.path])
    assert len(data) > 1048576 and len(canonical(manifest)) < 1024
    assert spool.load(op, manifest, [artifact.path]).artifacts == [artifact]
    with pytest.raises(ValueError, match="artifact_manifest_invalid"):
        spool.load(op, manifest, ["another.pt"])
    target = spool.path(op, manifest["artifacts"][0]["id"])
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="artifact_integrity_failed"):
        spool.load(op, manifest, [artifact.path])
    with pytest.raises(ValueError):
        spool.path("../other", str(uuid4()))


async def test_capacity_rejection_never_starts_another_run(execution: Any) -> None:
    service, sandbox, registry = execution
    service.max_active, sandbox.delay = 1, 100
    args = arguments(service)
    first = await prepare(service, registry, args)
    await service.submit(first, args)
    second = await prepare(service, registry, args)
    assert (await service.submit(second, args))["error_code"] == "experiment_capacity_reached"


async def test_adapter_source_mismatch_precedes_dispatch(
    execution: Any, experiment_http: ToolGateway
) -> None:
    service, sandbox, registry = execution
    adapter = MCPExperimentSandbox(experiment_http, service.workspace, service.spool, registry)
    with pytest.raises(SandboxError, match="source snapshot changed"):
        await adapter.run(
            SandboxRunRequest(
                task_id="file-task", action="static_check", files={"a.py": "not saved"}
            )
        )
    assert not sandbox.requests


async def test_terminal_workflow_allows_receipt_read_but_not_new_effect(execution: Any) -> None:
    service, _, _ = execution
    async with service.operations.sessions() as session:
        session.add(
            AgentTaskRow(
                id="finished-parent",
                objective="Completed fixture",
                status="succeeded",
                mode="autonomous",
            )
        )
        await session.flush()
        child = await session.get(AgentTaskRow, "file-task")
        assert child is not None
        child.parent_task_id, child.status = "finished-parent", "succeeded"
        session.add(
            WorkflowJobRow(
                task_id="finished-parent",
                idempotency_key="finished",
                request_sha256="0" * 64,
                status="succeeded",
                owner=None,
                generation=2,
                lease_until=None,
            )
        )
        await session.commit()
    ctx = context(parent_task_id="finished-parent")
    async with service.operations.sessions() as session:
        assert (await service.operations.authorize(session, ctx, read_only=True)).id == "file-task"
    async with service.operations.sessions() as session:
        with pytest.raises(OperationDenied, match="workflow_claim_required"):
            await service.operations.authorize(session, ctx)
    ctx["claim"] = {"task_id": "finished-parent", "owner": "old-worker", "generation": 1}
    async with service.operations.sessions() as session:
        with pytest.raises(OperationDenied, match="workflow_claim_stale"):
            await service.operations.authorize(session, ctx, read_only=True)


@pytest.mark.parametrize(
    "fault",
    ["approval", "source", "specification", "budget", "stale_worker", "coding_training", "valid"],
)
async def test_training_checks_authoritative_workflow_and_approval(
    execution: Any, fault: str
) -> None:
    service, sandbox, registry = execution
    service.approval_threshold = 0
    spec = ExperimentSpecification(epochs=1, train_samples=128, test_samples=128)
    upstream = {
        "train.py": "print('test fixture')\n",
        "test_models.py": "def test_fixture(): pass\n",
        "experiment_config.json": json.dumps(spec.model_dump(), indent=2) + "\n",
    }
    files = {**upstream, "experiment_config.json": json.dumps(spec.model_dump(), indent=2)}
    for path, content in files.items():
        service.workspace.write_text("file-task", path, content)
    snapshot = Snapshot(
        task_id="parent",
        objective="Offline approval",
        plan=plan(),
        sources={"code": upstream},
        specification=spec,
        stage="executor",
        results={"code": {"status": "succeeded"}},
        approval_threshold=0,
    )
    approval_payload = {
        "task_id": "parent",
        "plan_version": 1,
        "step_id": "train",
        "operation": "experiment",
        "risk_level": 3,
        "specification": spec.model_dump(),
        "source_sha256": source_hash(upstream),
        "budget_limits": snapshot.limits.model_dump(),
        "cost_units": cost_units(spec),
    }
    op_hash = digest(approval_payload)
    args = {
        **arguments(service),
        "action": "run_python",
        "path": "train.py",
        "collect_artifacts": list(ExperimentService.required_paths),
        "dataset_sha256": sandbox.dataset_sha256,
    }
    ctx = context(
        "execute",
        arguments_sha256=argument_digest(args),
        parent_task_id="parent",
        claim={"task_id": "parent", "owner": "worker", "generation": 1},
    )
    async with service.operations.sessions() as session:
        session.add(
            AgentTaskRow(
                id="parent", objective="Offline parent", status="running", mode="autonomous"
            )
        )
        await session.flush()
        child = await session.get(AgentTaskRow, "file-task")
        assert child is not None
        child.parent_task_id, child.mode = "parent", "experiment"
        session.add(
            WorkflowJobRow(
                task_id="parent",
                idempotency_key="approval-fixture",
                request_sha256="0" * 64,
                status="running",
                checkpoint_sequence=1,
                owner="worker",
                generation=1,
                lease_until=datetime.now(UTC) + timedelta(minutes=2),
                active={
                    "stage": "executor",
                    "version": 1,
                    "step_id": "train",
                    "operation_sha256": op_hash,
                },
                pending_calls={ctx["operation_id"]: "mcp:execute"},
                usage={"training_runs": 1, "sandbox_runs": 1, "tool_calls": 1},
            )
        )
        session.add(
            WorkflowCheckpointRow(
                id=str(uuid4()),
                task_id="parent",
                sequence=1,
                payload=snapshot.model_dump(mode="json"),
                sha256=digest(snapshot.model_dump(mode="json")),
            )
        )
        session.add(
            WorkflowApprovalRow(
                id=str(uuid4()),
                task_id="parent",
                operation_sha256=op_hash,
                payload=approval_payload,
                status="consumed",
                reason="Fixture",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.add(
            ExperimentRow(
                id="experiment",
                task_id="file-task",
                name=spec.name,
                status="running",
                specification=spec.model_dump(),
                source_sha256=args["source_sha256"],
                dataset_id="mnist",
                dataset_sha256=sandbox.dataset_sha256,
            )
        )
        await session.commit()
    await registry.prepare("experiment", ctx, REPLY)
    async with service.operations.sessions() as session:
        if fault == "approval":
            from sqlalchemy import select

            approval = await session.scalar(select(WorkflowApprovalRow))
            assert approval is not None
            approval.status = "approved"  # Core must consume, not the service.
        elif fault == "specification":
            experiment = await session.get(ExperimentRow, "experiment")
            assert experiment is not None
            experiment.specification = {**spec.model_dump(), "epochs": 2}
        elif fault in {"budget", "stale_worker", "coding_training"}:
            job = await session.get(WorkflowJobRow, "parent")
            assert job is not None
            if fault == "budget":
                job.usage = {"training_runs": 0}
            elif fault == "stale_worker":
                job.generation = 2
            else:
                job.active = {**job.active, "step_id": "code"}
        await session.commit()
    if fault == "source":
        service.workspace.edit_text("file-task", "train.py", "test fixture", "modified fixture")
    value = await service.submit(ctx, args)
    if fault == "valid":
        assert value["status"] == "queued", value
        assert (await completed(service, ctx))["status"] == "completed"
        assert len(sandbox.requests) == 1
        # The consumed approval cannot dispatch training again under a new call ID.
        ctx2 = dict(ctx, operation_id=str(uuid4()))
        async with service.operations.sessions() as session:
            job = await session.get(WorkflowJobRow, "parent")
            assert job is not None
            job.pending_calls = {**job.pending_calls, ctx2["operation_id"]: "mcp:execute"}
            await session.commit()
        await registry.prepare("experiment", ctx2, REPLY)
        assert (await service.submit(ctx2, args))["error_code"] == "training_already_dispatched"
    else:
        assert value["status"] == "denied", value
        assert not sandbox.requests
