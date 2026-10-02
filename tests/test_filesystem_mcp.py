import asyncio
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import AgentTaskRow, Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.agent import CodingAgent
from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager
from autoscholar.core.journal import current_journal
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.rag import database_models as rag_models  # noqa: F401
from autoscholar.tool_platform.context import ToolScope, tool_scope
from autoscholar.tool_platform.filesystem import file_contracts, mcp_file_tools
from autoscholar.tool_platform.filesystem_server import create_filesystem_app
from autoscholar.tool_platform.gateway import ToolGateway, ToolGatewayError, argument_digest
from autoscholar.tool_platform.operation_models import ToolOperationRow
from autoscholar.tool_platform.operations import OperationStore
from autoscholar.tool_platform.transport import MCPBackend
from tests.test_coding_agent import FakeSandbox, ScriptedCodingProvider, call, run_result

TOKEN = "offline-filesystem-service-token-32chars"


def context(name: str = "create_file", **overrides: Any) -> dict[str, Any]:
    return {
        "operation_id": str(uuid4()),
        "tool": name,
        "arguments_sha256": argument_digest({}),
        "scope": ToolScope("file-task").payload(),
        "parent_task_id": None,
        "claim": None,
        "deadline": time.time() + 30,
        **overrides,
    }


@pytest.fixture
async def filesystem(tmp_path: Path) -> AsyncIterator[tuple[OperationStore, WorkspaceManager]]:
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "operations.db").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(AgentTaskRow(id="file-task", objective="Offline fixture", status="running"))
        await session.commit()
    manager = WorkspaceManager(tmp_path / "workspaces", max_files=4, max_file_bytes=4096)
    manager.initialize("file-task")
    try:
        yield OperationStore(sessions, "filesystem"), manager
    finally:
        await engine.dispose()


@pytest.fixture
async def filesystem_http(
    filesystem: tuple[OperationStore, WorkspaceManager],
) -> AsyncIterator[tuple[ToolGateway, uvicorn.Server]]:
    operations, manager = filesystem
    app = create_filesystem_app(manager, operations, token=TOKEN, allowed_hosts=["127.0.0.1:*"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield (
            ToolGateway(
                MCPBackend(f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", TOKEN),
                file_contracts(manager),
                timeout_seconds=2,
            ),
            server,
        )
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 5)
        finally:
            sock.close()


async def test_real_http_file_lifecycle_scope_and_quota(
    filesystem_http: tuple[ToolGateway, uvicorn.Server],
    filesystem: tuple[OperationStore, WorkspaceManager],
) -> None:
    gateway, _ = filesystem_http
    _, manager = filesystem
    assert (await gateway.invoke("list_files", {}))["error_code"] == "task_scope_required"
    with tool_scope(ToolScope("missing-task")):
        assert (await gateway.invoke("list_files", {}))["error_code"] == "task_access_denied"
    with tool_scope(ToolScope("file-task")):
        for path in (
            "../secret",
            "../../.env",
            "D:/private",
            "../other/source/a.py",
            "a.py:secret",
            "NUL",
            "trailing. ",
            "nul\x00byte",
        ):
            assert not (await gateway.invoke("read_file", {"path": path}))["succeeded"]
        assert (await gateway.invoke("create_file", {"path": "a.py", "content": "中文 = 1\n"}))[
            "succeeded"
        ]
        read = await gateway.invoke("read_file", {"path": "a.py"})
        assert json.loads(read["output"])["content"] == "中文 = 1\n"
        assert (
            await gateway.invoke("edit_file", {"path": "a.py", "old_text": "1", "new_text": "2"})
        )["succeeded"]
        assert (
            len(json.loads((await gateway.invoke("search_code", {"query": "中文"}))["output"])) == 1
        )
        assert len(json.loads((await gateway.invoke("list_files", {}))["output"])) == 1
        assert (await gateway.invoke("delete_file", {"path": "a.py"}))["succeeded"]
        assert not manager.list_source_files("file-task")
        # UTF-8 bytes, not Unicode character count, determine the file-size quota.
        too_large = await gateway.invoke("create_file", {"path": "b.py", "content": "中" * 2000})
        assert too_large["error_code"] == "workspace_file_too_large"
        with pytest.raises(ToolGatewayError, match="tool_arguments_invalid"):
            await gateway.invoke("read_file", {"path": "a", "task_id": "another-task"})


@pytest.mark.parametrize("failure_type", [RuntimeError, PermissionError])
async def test_receipts_survive_restart_and_unknown_effect_is_not_replayed(
    filesystem: tuple[OperationStore, WorkspaceManager],
    failure_type: type[Exception],
) -> None:
    operations, manager = filesystem
    ctx, calls = context(), 0

    async def action() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        manager.write_text("file-task", "once.py", "one")
        return {"succeeded": True, "output": "created", "error_code": None, "uncertain": False}

    first = await operations.execute(ctx, action)
    restarted = OperationStore(operations.sessions, "filesystem")
    assert await restarted.execute(ctx, action) == first
    assert calls == 1
    conflict = await restarted.execute(dict(ctx, arguments_sha256="0" * 64), action)
    assert conflict["error_code"] == "operation_identity_conflict" and calls == 1
    crashed = context()

    async def crash_after_effect() -> dict[str, Any]:
        manager.write_text("file-task", "crashed.py", "effect committed")
        raise failure_type("simulated crash before receipt")

    with pytest.raises(failure_type):
        await operations.execute(crashed, crash_after_effect)
    assert manager.read_text("file-task", "crashed.py") == "effect committed"
    assert (await restarted.execute(crashed, action))["uncertain"] is True
    assert calls == 1
    async with operations.sessions() as session:
        row = await session.get(ToolOperationRow, crashed["operation_id"])
        assert row is not None and row.status == "running" and row.result is None


async def test_workflow_claim_and_cancel_fence(
    filesystem: tuple[OperationStore, WorkspaceManager],
) -> None:
    operations, _ = filesystem
    async with operations.sessions() as session:
        session.add(
            AgentTaskRow(id="parent", objective="Workflow", mode="autonomous", status="running")
        )
        await session.flush()
        task = await session.get(AgentTaskRow, "file-task")
        assert task is not None
        task.parent_task_id = "parent"
        session.add(
            WorkflowJobRow(
                task_id="parent",
                idempotency_key="fixture",
                request_sha256="0" * 64,
                status="running",
                owner="worker",
                generation=2,
                lease_until=datetime.now(UTC) + timedelta(seconds=60),
            )
        )
        await session.commit()
    calls = 0

    async def action() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"succeeded": True, "output": "ok", "error_code": None, "uncertain": False}

    ctx = context(
        parent_task_id="parent", claim={"task_id": "parent", "owner": "worker", "generation": 1}
    )
    assert (await operations.execute(ctx, action))["error_code"] == "workflow_claim_stale"
    ctx["claim"]["generation"] = 2
    assert (await operations.execute(ctx, action))["succeeded"] and calls == 1
    async with operations.sessions() as session:
        job = await session.get(WorkflowJobRow, "parent")
        assert job is not None
        job.status = "pause_requested"
        await session.commit()
    ctx["operation_id"] = str(uuid4())
    assert (await operations.execute(ctx, action))["succeeded"] and calls == 2
    async with operations.sessions() as session:
        job = await session.get(WorkflowJobRow, "parent")
        assert job is not None
        job.status = "cancel_requested"
        await session.commit()
    ctx["operation_id"] = str(uuid4())
    assert (await operations.execute(ctx, action))["error_code"] == "workflow_not_running"
    assert calls == 2


async def test_coding_agent_uses_remote_files_and_keeps_native_validation(
    filesystem: tuple[OperationStore, WorkspaceManager],
    filesystem_http: tuple[ToolGateway, uvicorn.Server],
) -> None:
    operations, manager = filesystem
    gateway, _ = filesystem_http
    provider = ScriptedCodingProvider(
        [
            call(
                "create",
                "create_file",
                {"path": "test_example.py", "content": "def test_ok(): assert True\n"},
            ),
            call("ready", "submit_code_ready", {"summary": "ready"}),
        ]
    )
    sandbox = FakeSandbox([run_result(), run_result()])
    agent = CodingAgent(
        provider=provider,
        repository=AgentTaskRepository(operations.sessions),
        workspaces=manager,
        sandbox=sandbox,
        filesystem_gateway=gateway,
    )
    events = []

    async def journal(op: str, kind: str, starting: bool) -> None:
        events.append((kind, starting))

    token = current_journal.set(journal)
    try:
        result = await agent.run(task_id="file-task", objective="Offline fixture", plan=["write"])
    finally:
        current_journal.reset(token)
    assert result.files_written == 1 and result.sandbox_runs == 2
    assert events == [("mcp:create_file", True), ("mcp:create_file", False)]
    assert len(sandbox.requests) == 2


async def test_uncertain_file_result_stops_coding_retry(
    filesystem: tuple[OperationStore, WorkspaceManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, manager = filesystem
    gateway = ToolGateway(MCPBackend("http://localhost:1/mcp", TOKEN), file_contracts(manager))

    async def uncertain(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ToolGatewayError("tool_timeout", uncertain=True)

    monkeypatch.setattr(gateway, "invoke", uncertain)
    tool = mcp_file_tools(manager, gateway, "file-task")[0]
    with pytest.raises(ToolGatewayError):
        await tool.execute({})


def test_task_and_in_tree_symlink_aliases_rejected(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "root")
    task = manager.initialize("one")
    manager.initialize("two")
    manager.write_text("one", "real.py", "private")
    try:
        (task / "source" / "alias.py").symlink_to(task / "source" / "real.py")
        (manager.root / "alias").symlink_to(task, target_is_directory=True)
    except OSError:
        pytest.skip("Symbolic links unavailable on this Windows account")
    for task_id, path in (("one", "alias.py"), ("alias", "real.py")):
        with pytest.raises(WorkspaceError):
            manager.read_text(task_id, path)
