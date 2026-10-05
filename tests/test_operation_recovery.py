import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.core.journal import current_journal
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.tool_platform.context import ToolScope, tool_scope
from autoscholar.tool_platform.filesystem import FILE_OUTPUT, file_contracts
from autoscholar.tool_platform.gateway import NativeBackend, ToolGateway
from autoscholar.tool_platform.operation_models import CoreToolCallRow, ToolOperationRow
from autoscholar.tool_platform.operations import OperationStore
from autoscholar.tool_platform.registry import InvocationRegistry
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_experiment_service import FakeExperimentSandbox

pytest_plugins = ["tests.test_filesystem_mcp"]


async def test_core_identity_commits_before_dispatch_and_receipt_is_bounded(
    filesystem: Any,
) -> None:
    operations, manager = filesystem
    registry = InvocationRegistry(operations.sessions)
    prepared: list[str] = []

    async def action(arguments: dict[str, Any]) -> dict[str, Any]:
        items = await registry.inspect("file-task")
        assert len(items) == 1 and items[0]["core_status"] == "prepared"
        prepared.append(items[0]["operation_id"])
        manager.write_text("file-task", arguments["path"], arguments["content"])
        return {"succeeded": True, "output": "ok", "error_code": None, "uncertain": False}

    contracts = file_contracts(manager)
    gateway = ToolGateway(
        NativeBackend(contracts, {"create_file": action}),
        contracts,
        registry=registry,
        service_name="filesystem",
    )
    events = []

    async def journal(op: str, kind: str, starting: bool) -> None:
        events.append((op, starting))

    token = current_journal.set(journal)
    try:
        with tool_scope(ToolScope("file-task")):
            await gateway.invoke("create_file", {"path": "once.py", "content": "one"})
    finally:
        current_journal.reset(token)
    assert events == [(prepared[0], True), (prepared[0], False)]
    items = await registry.inspect("file-task")
    assert items[0]["receipt_status"] == "completed"
    assert "content" not in str(items)


async def test_new_worker_queries_old_receipt_without_acquiring_old_write_authority(
    filesystem: Any,
) -> None:
    operations, _ = filesystem
    async with operations.sessions() as session:
        session.add(
            AgentTaskRow(id="parent", objective="Fixture", mode="autonomous", status="running")
        )
        await session.flush()
        task = await session.get(AgentTaskRow, "file-task")
        assert task
        task.parent_task_id, task.status = "parent", "failed"
        session.add(
            WorkflowJobRow(
                task_id="parent",
                idempotency_key="fixture",
                request_sha256="0" * 64,
                status="running",
                owner="new",
                generation=2,
                lease_until=datetime.now(UTC) + timedelta(seconds=60),
            )
        )
        original = {
            "scope": ToolScope("file-task").payload(),
            "parent_task_id": "parent",
            "claim": {"task_id": "parent", "owner": "old", "generation": 1},
        }
        op = str(uuid4())
        result = {"succeeded": True, "output": "done", "error_code": None, "uncertain": False}
        session.add(
            ToolOperationRow(
                id=op,
                task_id="file-task",
                service="filesystem",
                tool="create_file",
                arguments_sha256="0" * 64,
                authority_sha256=OperationStore.authority(original),
                status="completed",
                result=result,
            )
        )
        await session.commit()
    current = dict(
        original,
        claim={"task_id": "parent", "owner": "new", "generation": 2},
        deadline=time.time() + 20,
    )
    assert await operations.lookup(op, current) == {"status": "completed", "result": result}
    assert (await operations.lookup(op, dict(current, claim=original["claim"])))[
        "status"
    ] == "denied"
    with_other_scope = dict(current, scope=ToolScope("parent").payload(), parent_task_id=None)
    assert (await operations.lookup(op, with_other_scope))["status"] == "missing"


async def test_worker_reconciles_committed_step_without_replaying_file_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(
        tmp_path / "workspace",
        provider,
        FakeExperimentSandbox(),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}",
    )
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "recovery")
        await durable.tick(task_id)  # planner commits
        _, before = await durable.repository.snapshot(task_id)
        original_save = durable.repository.save

        async def interrupted(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("parent checkpoint not saved")

        monkeypatch.setattr(durable.repository, "save", interrupted)
        with pytest.raises(RuntimeError, match="parent checkpoint not saved"):
            await durable.tick(task_id)  # coding child commits; parent checkpoint is interrupted
        monkeypatch.setattr(durable.repository, "save", original_save)
        steps = await service.workflows.history(task_id, "steps")
        child_id = steps[0]["child_task_id"]
        operation_id = str(uuid4())
        result = {"succeeded": True, "output": "created", "error_code": None, "uncertain": False}
        async with durable.repository.sessions() as session:
            row = await session.get(WorkflowJobRow, task_id)
            assert row
            row.checkpoint_sequence = before.checkpoint_sequence
            row.status, row.owner, row.generation = "running", "old-worker", 10
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            row.active = {"stage": "executor", "version": 1, "step_id": "code"}
            row.pending_calls = {operation_id: "mcp:create_file"}
            session.add(
                CoreToolCallRow(
                    id=operation_id,
                    task_id=child_id,
                    parent_task_id=task_id,
                    service="filesystem",
                    tool="create_file",
                    arguments_sha256="0" * 64,
                    authority_sha256="1" * 64,
                    output_schema=FILE_OUTPUT,
                    status="prepared",
                )
            )
            session.add(
                ToolOperationRow(
                    id=operation_id,
                    task_id=child_id,
                    service="filesystem",
                    tool="create_file",
                    arguments_sha256="0" * 64,
                    authority_sha256="1" * 64,
                    status="completed",
                    result=result,
                )
            )
            await session.commit()
        used = dict(row.usage)
        calls_before = len(provider.calls)
        assert await DurableService(service).tick(task_id)
        restored, job = await durable.repository.snapshot(task_id)
        assert job.status == "queued" and not job.pending_calls
        assert restored.results["code"]["child_task_id"] == child_id
        assert job.usage == used and len(provider.calls) == calls_before
    finally:
        await engine.dispose()


async def test_missing_or_conflicting_receipt_does_not_clear_pending_call(tmp_path: Path) -> None:
    service, engine = await workflow(tmp_path, ScriptedProvider(script()), FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "missing")
        async with durable.repository.sessions() as session:
            job = await session.get(WorkflowJobRow, task_id)
            assert job
            job.status = "recovery_required"
            job.pending_calls = {"missing": "mcp:execute", "paid": "llm"}
            await session.commit()
        result = await durable.reconcile(task_id, 1)
        assert result["status"] == "recovery_required"
        assert result["pending_calls"] == {"missing": "mcp:execute", "paid": "llm"}
    finally:
        await engine.dispose()
