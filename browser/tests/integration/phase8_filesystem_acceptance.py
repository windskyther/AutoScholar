"""Offline Filesystem MCP acceptance: real PostgreSQL, two service processes, restarts.

Build autoscholar-api:phase8-test, then run this script with the project Python.
Only labelled private resources and the test script are used, never .env or API keys.
"""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


async def client(phase: str) -> None:
    from sqlalchemy import func, select

    from autoscholar.agent.database_models import AgentTaskRow
    from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager
    from autoscholar.infrastructure import Database
    from autoscholar.orchestration.durable_models import WorkflowJobRow
    from autoscholar.rag import database_models as rag_models  # noqa: F401
    from autoscholar.tool_platform.context import ToolScope, tool_scope
    from autoscholar.tool_platform.filesystem import file_contracts
    from autoscholar.tool_platform.gateway import ToolGateway, argument_digest
    from autoscholar.tool_platform.operation_models import ToolOperationRow
    from autoscholar.tool_platform.operations import OperationStore
    from autoscholar.tool_platform.transport import MCPBackend

    assert not os.getenv("LLM_API_KEY") and not os.getenv("TAVILY_API_KEY")
    database = Database(os.environ["DATABASE_URL"])
    manager = WorkspaceManager(Path("/workspaces"), max_files=3)
    backend = MCPBackend(
        "http://filesystem:8092/mcp", os.environ["MCP_SERVICE_TOKEN"], timeout_seconds=10
    )
    gateway = ToolGateway(backend, file_contracts(manager), timeout_seconds=10)
    fixed_id = "10000000-0000-4000-8000-000000000001"
    arguments = {"path": "once.py", "content": "one\n"}

    def ctx(task: str, tool: str, args: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        return {
            "operation_id": str(uuid4()),
            "tool": tool,
            "arguments_sha256": argument_digest(args),
            "scope": ToolScope(task).payload(),
            "parent_task_id": None,
            "claim": None,
            "deadline": time.time() + 20,
            **overrides,
        }

    async def invoke(tool: str, args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        async with backend.connect() as conn:
            reply = await conn.call(tool, args, context)
            assert not reply.is_error
            return reply.payload

    try:
        if phase == "prepare":
            for command in (["alembic", "upgrade", "head"], ["alembic", "check"]):
                migration = subprocess.run(command, capture_output=True, text=True, timeout=45)
                assert migration.returncode == 0, migration.stderr + migration.stdout
            async with database.session_factory() as session:
                for name in ("once", "quota", "links", "parent"):
                    session.add(
                        AgentTaskRow(id=name, objective="Offline fixture", status="running")
                    )
                    manager.initialize(name)
                await session.flush()
                session.add(
                    AgentTaskRow(
                        id="child",
                        parent_task_id="parent",
                        objective="Offline child",
                        status="running",
                    )
                )
                session.add(
                    WorkflowJobRow(
                        task_id="parent",
                        idempotency_key="fixture",
                        request_sha256="0" * 64,
                        status="running",
                        owner="worker-new",
                        generation=2,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                    )
                )
                await session.commit()
            manager.initialize("child")
        else:
            async with asyncio.timeout(30):
                while not await gateway.available():
                    await asyncio.sleep(0.1)
            context = ctx("once", "create_file", arguments, operation_id=fixed_id)
            result = await invoke("create_file", arguments, context)
            assert result["succeeded"], result
            assert manager.read_text("once", "once.py") == "one\n"
            # Same identifier, same identity, even after a new service process starts.
            assert await invoke("create_file", arguments, context) == result
            changed = dict(arguments, content="two")
            conflict = await invoke(
                "create_file", changed, ctx("once", "create_file", changed, operation_id=fixed_id)
            )
            assert conflict["error_code"] == "operation_identity_conflict"
            assert manager.read_text("once", "once.py") == "one\n"
            if phase == "healthy":
                # Cross-process quota: 8 concurrent calls may create at most 3 files.
                async def write(index: int) -> dict[str, Any]:
                    args = {"path": f"{index}.py", "content": str(index)}
                    return await invoke("create_file", args, ctx("quota", "create_file", args))

                results = await asyncio.gather(*(write(i) for i in range(8)))
                assert sum(r["succeeded"] for r in results) == 3, results
                assert len(manager.list_source_files("quota")) == 3
                assert all(
                    r["succeeded"] or r["error_code"] == "workspace_file_limit" for r in results
                )
                # Concurrent duplicates share one reservation and never execute twice.
                args = {"path": "parallel.py", "content": "parallel"}
                duplicate = ctx("once", "create_file", args)
                responses = await asyncio.gather(
                    *(invoke("create_file", args, duplicate) for _ in range(8))
                )
                assert any(r["succeeded"] for r in responses)
                assert all(r["succeeded"] or r.get("uncertain") for r in responses)
                assert (await invoke("create_file", args, duplicate))["succeeded"]
                # Reserve, produce the effect, then interrupt before the DB receipt commit.
                store = OperationStore(database.session_factory, "filesystem")
                unknown = ctx(
                    "once", "edit_file", {"path": "once.py", "old_text": "one", "new_text": "one!"}
                )

                async def crash() -> dict[str, Any]:
                    manager.edit_text("once", "once.py", "one", "one!")
                    raise RuntimeError("crash before receipt")

                try:
                    await store.execute(unknown, crash)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Crash fixture did not interrupt")
                response = await invoke(
                    "edit_file", {"path": "once.py", "old_text": "one", "new_text": "one!"}, unknown
                )
                assert response["uncertain"]
                assert manager.read_text("once", "once.py") == "one!\n", "Unknown edit was repeated"
                # Restore fixture through a distinct, explicitly requested edit for restart checks.
                manager.edit_text("once", "once.py", "one!", "one")
                args = {"path": "stale.py", "content": "no"}
                stale = ctx(
                    "child",
                    "create_file",
                    args,
                    parent_task_id="parent",
                    claim={"task_id": "parent", "owner": "worker-old", "generation": 1},
                )
                assert (await invoke("create_file", args, stale))[
                    "error_code"
                ] == "workflow_claim_stale"
                assert not manager.list_source_files("child")
                stale["claim"] = {"task_id": "parent", "owner": "worker-new", "generation": 2}
                assert (await invoke("create_file", args, stale))["succeeded"]
                async with database.session_factory() as session:
                    job = await session.get(WorkflowJobRow, "parent")
                    assert job is not None
                    job.status = "cancel_requested"
                    await session.commit()
                args = {"path": "cancel.py", "content": "no"}
                assert (
                    await invoke(
                        "create_file",
                        args,
                        ctx(
                            "child",
                            "create_file",
                            args,
                            parent_task_id="parent",
                            claim=stale["claim"],
                        ),
                    )
                )["error_code"] == "workflow_not_running"
                # Linux symlink tests cover same-tree aliases and cross-task directory aliases.
                root = manager.root / "links"
                manager.write_text("links", "real.py", "private")
                (root / "source" / "alias.py").symlink_to(root / "source" / "real.py")
                (manager.root / "alias").symlink_to(root, target_is_directory=True)
                with tool_scope(ToolScope("links")):
                    assert not (await gateway.invoke("read_file", {"path": "alias.py"}))[
                        "succeeded"
                    ]
                try:
                    manager.read_text("alias", "real.py")
                except WorkspaceError:
                    pass
                else:
                    raise AssertionError("Task directory alias was accepted")
            async with database.session_factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(ToolOperationRow)
                        .where(ToolOperationRow.id == fixed_id)
                    )
                    == 1
                )
        print(
            json.dumps({"phase": phase, "acceptance": "passed", "external_api_calls": 0}),
            flush=True,
        )
    finally:
        await database.close()


def orchestrate() -> None:
    from phase8_research_acceptance import docker  # type: ignore[import-not-found]

    tag = "phase8-filesystem-" + uuid4().hex[:10]
    label, image = "autoscholar.acceptance=" + tag, "autoscholar-api:phase8-test"
    env = dict(os.environ, MCP_SERVICE_TOKEN=secrets.token_urlsafe(32))
    containers: list[str] = []
    network_created = volume_created = False
    script = Path(__file__).resolve().as_posix()
    common = [
        "-e",
        "MCP_SERVICE_TOKEN",
        "-e",
        "DATABASE_URL=postgresql+asyncpg://fixture:fixture@postgres:5432/fixture",
        "--mount",
        f"type=volume,source={tag},target=/workspaces",
        "--mount",
        f"type=bind,source={script},target=/tmp/acceptance.py,readonly",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16777216",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "768m",
        "--pids-limit",
        "128",
    ]

    def create(name: str, *args: str) -> str:
        cid = docker(
            "create", "--name", tag + name, "--label", label, "--network", tag, *args, env=env
        )
        containers.append(cid)
        return str(cid)

    def actor(phase: str) -> None:
        cid = create("-" + phase, *common, image, "python", "/tmp/acceptance.py", "--client", phase)
        docker("start", cid)
        code = docker("wait", cid)
        print(docker("logs", cid), flush=True)
        assert code == "0", "Filesystem acceptance client failed"

    try:
        docker("network", "create", "--internal", "--label", label, tag)
        network_created = True
        docker("volume", "create", "--label", label, tag)
        volume_created = True
        pg = create(
            "-postgres",
            "--network-alias",
            "postgres",
            "-e",
            "POSTGRES_USER=fixture",
            "-e",
            "POSTGRES_PASSWORD=fixture",
            "-e",
            "POSTGRES_DB=fixture",
            "postgres:16-alpine",
        )
        docker("start", pg)
        deadline = time.monotonic() + 30
        while "accepting connections" not in docker(
            "exec", pg, "pg_isready", "-U", "fixture", check=False
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("PostgreSQL startup timeout")
            time.sleep(0.2)
        actor("prepare")
        server = create(
            "-server",
            *common,
            "--network-alias",
            "filesystem",
            "-e",
            "WORKSPACE_ROOT=/workspaces",
            "-e",
            "WORKSPACE_MAX_FILES=3",
            "-e",
            'MCP_ALLOWED_HOSTS=["filesystem:8092"]',
            image,
            "uvicorn",
            "autoscholar.tool_platform.filesystem_server:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8092",
            "--workers",
            "2",
            "--no-access-log",
        )
        docker("start", server)
        actor("healthy")
        docker("restart", "--time", "10", server)
        actor("restarted")
    finally:
        for cid in reversed(containers):
            record = json.loads(docker("inspect", cid))[0]
            assert record["Config"]["Labels"].get("autoscholar.acceptance") == tag
            docker("rm", "--force", "--volumes", cid)
        if volume_created:
            record = json.loads(docker("volume", "inspect", tag))[0]
            assert record["Labels"].get("autoscholar.acceptance") == tag
            docker("volume", "rm", tag)
        if network_created:
            record = json.loads(docker("network", "inspect", tag))[0]
            assert record["Labels"].get("autoscholar.acceptance") == tag
            docker("network", "rm", tag)
        print(json.dumps({"cleanup": "passed", "isolated_stack": tag}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=["prepare", "healthy", "restarted"])
    options = parser.parse_args()
    if options.client:
        asyncio.run(client(options.client))
    else:
        orchestrate()
