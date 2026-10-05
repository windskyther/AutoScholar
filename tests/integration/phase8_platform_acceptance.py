"""Private PostgreSQL + all MCP processes + real Docker CPU acceptance, no paid APIs.

Run from the host with project Python after building phase8-test API/Git images.
Only this script and its Research fixture are mounted; .env is never passed through.
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

URL = "https://example.org/fixture/research.git"


def git_serve() -> None:
    import uvicorn

    from autoscholar.coding.workspace import WorkspaceManager
    from autoscholar.infrastructure import Database
    from autoscholar.tool_platform.git_manager import GitManager
    from autoscholar.tool_platform.git_server import create_git_app
    from autoscholar.tool_platform.operations import OperationStore

    holder: list[GitManager] = []

    async def fetch(url: str, stage: Path, guard: Any) -> None:
        assert url == URL
        await holder[0].run(
            [
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--no-checkout",
                "--no-hardlinks",
                "--template=",
                "--",
                "/git/fixture-origin",
                str(stage / "repository"),
            ],
            stage,
            timeout=10,
            guard=guard,
        )

    database = Database(os.environ["DATABASE_URL"])
    manager = GitManager(
        Path("/git/metadata"),
        WorkspaceManager(Path("/workspaces")),
        allowed_urls=[URL],
        fetcher=fetch,
    )
    holder.append(manager)
    app = create_git_app(
        manager,
        OperationStore(database.session_factory, "git"),
        token=os.environ["MCP_SERVICE_TOKEN"],
        allowed_hosts=["git:8093"],
    )
    uvicorn.run(app, host="0.0.0.0", port=8093, log_level="error", access_log=False)


class HealthyDependency:
    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass


async def client(phase: str) -> None:
    import httpx
    from pydantic import SecretStr
    from sqlalchemy import select

    from autoscholar.coding.sandbox import SandboxRunRequest
    from autoscholar.coding.workspace import WorkspaceManager
    from autoscholar.core.budget import current_parent
    from autoscholar.core.config import Settings
    from autoscholar.infrastructure import Database
    from autoscholar.llm.models import LLMResult, TokenUsage, ToolCall
    from autoscholar.main import create_app
    from autoscholar.orchestration.approvals import ApprovalDecision
    from autoscholar.orchestration.smoke import OfflineCoordinator
    from autoscholar.rag.database_models import DocumentChunkRow, DocumentRow
    from autoscholar.rag.models import RetrievedChunk
    from autoscholar.tool_platform.context import ToolScope, tool_scope
    from autoscholar.tool_platform.experiment_contracts import REPLY
    from autoscholar.tool_platform.experiment_service import source_hash
    from autoscholar.tool_platform.gateway import ToolGatewayError, argument_digest
    from autoscholar.tool_platform.operation_models import CoreToolCallRow, ExperimentExecutionRow
    from autoscholar.tool_platform.registry import InvocationRegistry

    assert not os.getenv("LLM_API_KEY") and not os.getenv("TAVILY_API_KEY")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=os.environ["DATABASE_URL"],
        log_level="WARNING",
        workspace_root=Path("/workspaces"),
        mcp_artifact_root=Path("/spool"),
        document_storage_path=Path("/tmp/documents"),
        research_tool_backend="mcp",
        filesystem_tool_backend="mcp",
        git_tool_backend="mcp",
        experiment_tool_backend="mcp",
        mcp_research_url="http://research:8091/mcp",
        mcp_filesystem_url="http://filesystem:8092/mcp",
        mcp_git_url="http://git:8093/mcp",
        mcp_experiment_url="http://experiment:8094/mcp",
        mcp_service_token=SecretStr(os.environ["MCP_SERVICE_TOKEN"]),
        experiment_api_token=SecretStr("isolated-core-api-token"),
        workflow_approval_threshold=0,
    )
    database = Database(settings.database_url)
    workspace = WorkspaceManager(settings.workspace_root)

    class IndexedFixture:
        async def retrieve(
            self, question: str, *, project_id: str, **kwargs: Any
        ) -> list[RetrievedChunk]:
            async with database.session_factory() as session:
                pairs = (
                    await session.execute(
                        select(DocumentChunkRow, DocumentRow)
                        .join(DocumentRow, DocumentRow.id == DocumentChunkRow.document_id)
                        .where(DocumentRow.project_id == project_id, DocumentRow.status == "ready")
                    )
                ).all()
                return [
                    RetrievedChunk(
                        id=chunk.id,
                        project_id=project_id,
                        document_id=document.id,
                        title=document.title,
                        page=chunk.page,
                        section=chunk.section,
                        content=chunk.content,
                        score=0.9,
                        ordinal=chunk.ordinal,
                    )
                    for chunk, document in pairs
                ]

        async def query(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("The acceptance uses only indexed evidence retrieval")

    class Coordinator(OfflineCoordinator):
        edited = False

        async def generate(
            self, messages: Any, *, tools: Any = None, tool_choice: Any = "none"
        ) -> LLMResult:
            names = {tool.name for tool in (tools or [])}
            arguments: dict[str, Any]
            if "submit_plan" in names:
                name, arguments = (
                    "submit_plan",
                    {"mode": "research", "steps": ["Verify public method evidence"]},
                )
            elif "submit_research_queries" in names:
                name, arguments = (
                    "submit_research_queries",
                    {
                        "queries": [
                            {
                                "topic": "MNIST method",
                                "query": query,
                                "source_type": "web",
                                "purpose": "Offline method fixture",
                            }
                            for query in ("MLP MNIST", "CNN MNIST", "MNIST comparison")
                        ]
                    },
                )
            elif "submit_evidence" in names:
                candidates = json.loads(
                    messages[-1]
                    .content.split("<untrusted_sources>\n")[1]
                    .split("</untrusted_sources>")[0]
                )
                name, arguments = (
                    "submit_evidence",
                    {
                        "items": [
                            {
                                "candidate_id": item["candidate_id"],
                                "claim": item["content"],
                                "relevance": 0.9,
                            }
                            for item in candidates
                        ]
                    },
                )
            elif "submit_research_report" in names:
                name, arguments = (
                    "submit_research_report",
                    {
                        "answer": "Offline public and indexed fixture evidence [E1] [E2].",
                        "citations": [
                            {
                                "claim": "Offline public and indexed fixture evidence",
                                "evidence_ids": ["E1", "E2"],
                            }
                        ],
                    },
                )
            elif "submit_code_ready" in names and not self.edited:
                self.edited = True
                name, arguments = (
                    "edit_file",
                    {
                        "path": "train.py",
                        "old_text": "from pathlib import Path",
                        "new_text": "from pathlib import Path\n# Offline MCP boundary acceptance.",
                    },
                )
            else:
                result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
                if "submit_task_plan" in names:
                    payload = dict(result.tool_calls[0].arguments)
                    payload["steps"] = [
                        {
                            "id": "research",
                            "type": "research",
                            "description": "Check public method evidence",
                            "expected_output": "Verified citations",
                        },
                        *payload["steps"],
                    ]
                    payload["steps"][1]["dependencies"] = ["research"]
                    return replace(
                        result,
                        tool_calls=(
                            ToolCall(id=str(uuid4()), name="submit_task_plan", arguments=payload),
                        ),
                    )
                return result
            return LLMResult(
                text="",
                model="offline-mcp-coordinator",
                usage=TokenUsage(0, 0, 0),
                tool_calls=(ToolCall(id=str(uuid4()), name=name, arguments=arguments),),
            )

    app = create_app(
        settings,
        database=database,
        redis=HealthyDependency(),
        qdrant=HealthyDependency(),
        llm_provider=Coordinator(),
        rag_query_service=IndexedFixture(),
    )
    state_path = workspace.root / "acceptance.json"
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture",
                headers={"Authorization": "Bearer isolated-core-api-token"},
            ) as api,
            asyncio.timeout(180),
        ):
            durable = app.state.durable_service
            assert durable is not None
            gateways = [
                app.state.mcp_research_gateway,
                app.state.mcp_filesystem_gateway,
                app.state.mcp_git_gateway,
                app.state.mcp_experiment_gateway,
            ]
            for gateway in gateways:
                async with asyncio.timeout(30):
                    while not await gateway.available():
                        await asyncio.sleep(0.1)
            if phase == "prepare":
                git_task = "git-" + uuid4().hex[:16]
                await app.state.agent_repository.create_task(
                    task_id=git_task,
                    objective="Offline Git to Filesystem to Experiment validation",
                    mode="coding",
                )
                workspace.initialize(git_task)
                with tool_scope(ToolScope(git_task)):
                    cloned = await app.state.mcp_git_gateway.invoke("clone_repo", {"repo_url": URL})
                    assert cloned["succeeded"], cloned
                    prefix = json.loads(cloned["output"])["repo_id"]
                    file_gateway = app.state.mcp_filesystem_gateway
                    assert (await file_gateway.invoke("read_file", {"path": prefix + "/model.py"}))[
                        "succeeded"
                    ]
                    assert (
                        await file_gateway.invoke(
                            "edit_file",
                            {
                                "path": prefix + "/model.py",
                                "old_text": "value = 1",
                                "new_text": "value = 2",
                            },
                        )
                    )["succeeded"]
                    status = await app.state.mcp_git_gateway.invoke(
                        "git_status", {"repo_id": prefix}
                    )
                    diff = await app.state.mcp_git_gateway.invoke("git_diff", {"repo_id": prefix})
                    assert "model.py" in status["output"] and "+value = 2" in diff["output"]
                    for action in ("static_check", "run_pytest"):
                        result = await app.state.sandbox_executor.run(
                            SandboxRunRequest(
                                task_id=git_task,
                                action=action,
                                files=workspace.source_snapshot(git_task),
                                timeout_seconds=30,
                            )
                        )
                        assert result.status == "succeeded", result
                project = await api.post(
                    "/projects", json={"name": "Phase 8 offline MCP acceptance"}
                )
                project.raise_for_status()
                async with database.session_factory() as session:
                    document_id = str(uuid4())
                    session.add(
                        DocumentRow(
                            id=document_id,
                            project_id=project.json()["id"],
                            title="Indexed fixture",
                            original_filename="fixture.pdf",
                            content_type="application/pdf",
                            storage_key="fixture",
                            sha256="0" * 64,
                            size_bytes=1,
                            status="ready",
                            chunk_count=1,
                        )
                    )
                    await session.flush()
                    session.add(
                        DocumentChunkRow(
                            id=str(uuid4()),
                            project_id=project.json()["id"],
                            document_id=document_id,
                            ordinal=0,
                            page=1,
                            content="Offline indexed fixture evidence",
                            token_count=5,
                        )
                    )
                    await session.commit()
                task_id, _ = await durable.submit(
                    {
                        "objective": (
                            "Verify public research, seeded MNIST CPU code, metrics "
                            "and artifacts through MCP"
                        ),
                        "project_id": project.json()["id"],
                        "research_sources": ["web"],
                        "experiment_specification": {
                            "epochs": 1,
                            "train_samples": 128,
                            "test_samples": 128,
                        },
                    },
                    str(uuid4()),
                )
                for _ in range(3):
                    assert await durable.tick(task_id)
                snapshot, job = await durable.repository.snapshot(task_id)
                assert snapshot.results["research"]["status"] == "succeeded", snapshot.results
                research_child = await app.state.agent_repository.get_task(
                    snapshot.results["research"]["child_task_id"]
                )
                assert research_child is not None
                assert any(
                    item.source_type == "document" and len(item.query) > 400
                    for item in research_child.evidence
                )
                assert snapshot.results["code"]["status"] == "succeeded", snapshot.results
                await durable.repository.control(task_id, "pause")
                state_path.write_text(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "coding_child": snapshot.results["code"]["child_task_id"],
                            "usage": job.usage,
                            "git_task": git_task,
                            "repo_id": prefix,
                        }
                    ),
                    encoding="utf-8",
                )
                print(
                    json.dumps({"phase": phase, "acceptance": "passed", "external_api_calls": 0}),
                    flush=True,
                )
                return
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if phase == "resume":
                task_id = state["task_id"]
                snapshot, job = await durable.repository.snapshot(task_id)
                assert job.status == "paused" and job.usage == state["usage"]
                await durable.repository.control(task_id, "resume")
                approvals = 0
                for _ in range(12):
                    await durable.tick(task_id)
                    snapshot, job = await durable.repository.snapshot(task_id)
                    if job.status == "awaiting_approval":
                        pending = [
                            item
                            for item in await durable.approvals.list(task_id)
                            if item["status"] == "pending"
                        ]
                        assert len(pending) == 1
                        approval = pending[0]
                        await durable.approvals.decide(
                            task_id,
                            approval["id"],
                            ApprovalDecision(
                                action="approve",
                                operation_sha256=approval["operation_sha256"],
                                reason="Bounded offline CPU acceptance",
                            ),
                        )
                        await durable.repository.control(task_id, "resume")
                        approvals += 1
                    elif job.status != "queued":
                        break
                assert job.status == "succeeded", (job.status, job.error_code, snapshot.results)
                assert approvals == 1 and job.usage["training_runs"] == 1 and not job.pending_calls
                assert snapshot.results["code"]["child_task_id"] == state["coding_child"]
                child = snapshot.results["train"]["child_task_id"]
                response = await api.get(f"/agent/tasks/{child}/artifacts")
                response.raise_for_status()
                items = response.json()["items"]
                assert len(items) == 10
                for item in items:
                    download = await api.get(f"/agent/tasks/{child}/artifacts/{item['id']}")
                    download.raise_for_status()
                    assert (
                        len(download.content) == item["size_bytes"]
                        and hashlib.sha256(download.content).hexdigest() == item["sha256"]
                    )
                calls = (await api.get(f"/agent/tasks/{task_id}/operations")).json()["items"]
                assert {item["service"] for item in calls} == {
                    "research",
                    "filesystem",
                    "experiment",
                }, calls
                assert all(item["receipt_status"] == "completed" for item in calls)
                async with database.session_factory() as session:
                    rows = (
                        await session.scalars(
                            select(CoreToolCallRow).where(
                                CoreToolCallRow.task_id == child,
                                CoreToolCallRow.service == "experiment",
                            )
                        )
                    ).all()
                    training = next(
                        row for row in rows if row.result and row.result["result"]["artifacts"]
                    )
                    state["training_operation"] = training.id
                state["training_child"] = child
                state_path.write_text(json.dumps(state), encoding="utf-8")
                print(
                    json.dumps(
                        {
                            "phase": phase,
                            "acceptance": "passed",
                            "training_runs": 1,
                            "artifacts_verified": 10,
                            "reused_coding_child": state["coding_child"],
                            "external_api_calls": 0,
                        }
                    ),
                    flush=True,
                )
            elif phase == "receipt":
                parent = current_parent.set(state["task_id"])
                with tool_scope(ToolScope(state["training_child"])):
                    # Completed task reads are allowed; writes remain forbidden.
                    for tool in ("get_status", "get_logs", "get_metrics"):
                        value = await app.state.mcp_experiment_gateway.invoke(
                            tool, {"operation_id": state["training_operation"]}, journal=False
                        )
                        assert value["status"] == "completed" and value["result"]["artifacts"]
                        if tool == "get_metrics":
                            assert len(value["metrics"]["runs"]) == 2
                current_parent.reset(parent)
                print(
                    json.dumps({"phase": phase, "acceptance": "passed", "external_api_calls": 0}),
                    flush=True,
                )
            elif phase == "interrupt":
                task = "interrupt-" + uuid4().hex[:12]
                await app.state.agent_repository.create_task(
                    task_id=task, objective="Bounded interruption fixture", mode="coding"
                )
                workspace.initialize(task)
                workspace.write_text(task, "wait.py", "import time\ntime.sleep(60)\n")
                args = {
                    "action": "run_python",
                    "path": "wait.py",
                    "args": [],
                    "collect_artifacts": [],
                    "timeout_seconds": 60,
                    "source_sha256": source_hash(workspace.source_snapshot(task)),
                    "dataset_sha256": None,
                }
                operation = str(uuid4())
                ctx = {
                    "operation_id": operation,
                    "parent_task_id": None,
                    "tool": "execute",
                    "arguments_sha256": argument_digest(args),
                    "deadline": time.time() + 30,
                    "scope": ToolScope(task).payload(),
                    "claim": None,
                }
                await InvocationRegistry(database.session_factory).prepare("experiment", ctx, REPLY)
                with tool_scope(ToolScope(task)):
                    response = await app.state.mcp_experiment_gateway.invoke(
                        "execute", args, operation_id=operation, journal=False
                    )
                    assert response["status"] == "queued"
                    async with asyncio.timeout(20):
                        while True:
                            response = await app.state.mcp_experiment_gateway.invoke(
                                "get_status", {"operation_id": operation}, journal=False
                            )
                            if response["status"] == "running":
                                break
                            await asyncio.sleep(0.1)
                state.update(interrupted_task=task, interrupted_operation=operation)
                state_path.write_text(json.dumps(state), encoding="utf-8")
                print(
                    json.dumps(
                        {
                            "phase": phase,
                            "task_id": task,
                            "acceptance": "passed",
                            "external_api_calls": 0,
                        }
                    ),
                    flush=True,
                )
            elif phase == "uncertain":
                with tool_scope(ToolScope(state["interrupted_task"])):
                    async with asyncio.timeout(25):
                        while True:
                            try:
                                await app.state.mcp_experiment_gateway.invoke(
                                    "get_status",
                                    {"operation_id": state["interrupted_operation"]},
                                    journal=False,
                                )
                            except ToolGatewayError as exc:
                                assert exc.code == "tool_result_uncertain" and exc.uncertain
                                break
                            await asyncio.sleep(0.2)
                async with database.session_factory() as session:
                    assert (
                        len(
                            (
                                await session.scalars(
                                    select(ExperimentExecutionRow)
                                    .join(
                                        CoreToolCallRow,
                                        CoreToolCallRow.id == ExperimentExecutionRow.id,
                                    )
                                    .where(CoreToolCallRow.task_id == state["interrupted_task"])
                                )
                            ).all()
                        )
                        == 1
                    )
                print(
                    json.dumps(
                        {
                            "phase": phase,
                            "acceptance": "passed",
                            "unknown_execution_not_replayed": True,
                            "external_api_calls": 0,
                        }
                    ),
                    flush=True,
                )
    finally:
        await database.close()


def orchestrate() -> None:
    from importlib import import_module

    docker_command = import_module("phase8_research_acceptance").docker

    def docker(*arguments: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        if arguments[0] == "logs":
            result = subprocess.run(
                ["docker", *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            return result.stdout + result.stderr
        return str(docker_command(*arguments, env=env, check=check))

    tag = "phase8-platform-" + uuid4().hex[:10]
    label = "autoscholar.acceptance=" + tag
    env = dict(os.environ, MCP_SERVICE_TOKEN=secrets.token_urlsafe(32))
    api_image, git_image = "autoscholar-api:phase8-test", "autoscholar-git:phase8-test"
    script = Path(__file__).resolve().as_posix()
    research_script = Path(__file__).with_name("phase8_research_acceptance.py").resolve().as_posix()
    containers: list[str] = []
    volumes: list[str] = []
    network_created = False
    common = [
        "-e",
        "MCP_SERVICE_TOKEN",
        "-e",
        "DATABASE_URL=postgresql+asyncpg://fixture:fixture@postgres:5432/fixture",
        "-e",
        "DOCUMENT_STORAGE_PATH=/tmp/documents",
        "-e",
        "MODEL_CACHE_PATH=/tmp/models",
        "--mount",
        f"type=bind,source={script},target=/tmp/acceptance.py,readonly",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=33554432",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "128",
        "--memory",
        "1g",
    ]

    def mount(area: str, target: str, *, readonly: bool = False) -> list[str]:
        return [
            "--mount",
            f"type=volume,source={tag}-{area},target={target}" + (",readonly" if readonly else ""),
        ]

    def create(name: str, *args: str) -> str:
        cid = docker(
            "create", "--name", tag + name, "--label", label, "--network", tag, *args, env=env
        )
        containers.append(cid)
        return str(cid)

    def actor(phase: str) -> dict[str, Any]:
        cid = create(
            "-" + phase,
            *common,
            *mount("workspace", "/workspaces"),
            *mount("spool", "/spool", readonly=True),
            *mount("git", "/git"),
            api_image,
            "python",
            "/tmp/acceptance.py",
            "--client",
            phase,
        )
        docker("start", cid)
        code = docker("wait", cid)
        logs = docker("logs", cid)
        if code != "0":
            print(logs[-20000:], flush=True)
        assert code == "0", "Platform acceptance client failed"
        result: dict[str, Any] = next(
            json.loads(line) for line in reversed(logs.splitlines()) if line.startswith('{"phase":')
        )
        print(json.dumps(result), flush=True)
        return result

    try:
        docker("network", "create", "--internal", "--label", label, tag)
        network_created = True
        for area in ("workspace", "spool", "git"):
            docker("volume", "create", "--label", label, tag + "-" + area)
            volumes.append(tag + "-" + area)
        postgres = create(
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
        docker("start", postgres)
        deadline = time.monotonic() + 30
        while "accepting connections" not in docker(
            "exec", postgres, "pg_isready", "-U", "fixture", check=False
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("Private PostgreSQL did not start")
            time.sleep(0.2)
        migration = create(
            "-migrate", *common, api_image, "sh", "-c", "alembic upgrade head && alembic check"
        )
        docker("start", migration)
        assert docker("wait", migration) == "0", docker("logs", migration)
        origin = create(
            "-origin",
            *common,
            *mount("git", "/git"),
            git_image,
            "python",
            "/tmp/acceptance.py",
            "--origin",
        )
        docker("start", origin)
        assert docker("wait", origin) == "0", docker("logs", origin)
        manager = create(
            "-sandbox",
            "--network-alias",
            "sandbox",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=volume,source=autoscholar_mnist_data,target=/datasets/mnist,readonly",
            "-e",
            "SANDBOX_IMAGE=autoscholar-python-sandbox:phase4",
            "-e",
            "SANDBOX_DATASET_VOLUME=autoscholar_mnist_data",
            "-e",
            "SANDBOX_DATASET_READY_FILE=/datasets/mnist/.autoscholar-ready",
            api_image,
            "uvicorn",
            "autoscholar.sandbox.manager:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8090",
            "--no-access-log",
        )
        research = create(
            "-research",
            *common,
            "--network-alias",
            "research",
            "-e",
            "ACCEPTANCE_DATABASE_URL=postgresql+asyncpg://fixture:fixture@postgres:5432/fixture",
            "--mount",
            f"type=bind,source={research_script},target=/tmp/research.py,readonly",
            api_image,
            "python",
            "/tmp/research.py",
            "--serve",
        )
        filesystem = create(
            "-filesystem",
            *common,
            *mount("workspace", "/workspaces"),
            "--network-alias",
            "filesystem",
            "-e",
            "WORKSPACE_ROOT=/workspaces",
            "-e",
            'MCP_ALLOWED_HOSTS=["filesystem:8092"]',
            api_image,
            "uvicorn",
            "autoscholar.tool_platform.filesystem_server:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8092",
            "--no-access-log",
        )
        git = create(
            "-git",
            *common,
            *mount("workspace", "/workspaces"),
            *mount("git", "/git"),
            "--network-alias",
            "git",
            git_image,
            "python",
            "/tmp/acceptance.py",
            "--git-server",
        )
        experiment = create(
            "-experiment",
            *common,
            *mount("workspace", "/workspaces", readonly=True),
            *mount("spool", "/spool"),
            "--network-alias",
            "experiment",
            "-e",
            "WORKSPACE_ROOT=/workspaces",
            "-e",
            "MCP_ARTIFACT_ROOT=/spool",
            "-e",
            "SANDBOX_MANAGER_URL=http://sandbox:8090",
            "-e",
            "WORKFLOW_APPROVAL_THRESHOLD=0",
            "-e",
            'MCP_ALLOWED_HOSTS=["experiment:8094"]',
            api_image,
            "uvicorn",
            "autoscholar.tool_platform.experiment_server:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8094",
            "--no-access-log",
        )
        for cid in (manager, research, filesystem, git, experiment):
            docker("start", cid)
        actor("prepare")
        for cid in (research, filesystem, git, experiment):
            docker("restart", "--time", "10", cid)
        actor("resume")
        docker("restart", "--time", "10", experiment)
        actor("receipt")
        interrupted_state = actor("interrupt")
        # Capture exact task-owned sandbox IDs and their temporary volume before killing MCP.
        deadline = time.monotonic() + 20
        interrupted: list[dict[str, Any]] = []
        while not interrupted:
            ids = docker(
                "ps",
                "-q",
                "--filter",
                "name=autoscholar-" + interrupted_state["task_id"][:24] + "-",
            ).splitlines()
            interrupted = [json.loads(docker("inspect", cid))[0] for cid in ids]
            if time.monotonic() > deadline:
                raise TimeoutError("Interruption sandbox did not start")
            time.sleep(0.1)
        assert len(interrupted) == 1
        owned_volume = next(
            m["Name"] for m in interrupted[0]["Mounts"] if m["Destination"] == "/workspace"
        )
        record = json.loads(docker("volume", "inspect", owned_volume))[0]
        assert record["Labels"].get("autoscholar.temporary") == "true"
        docker("kill", "--signal", "KILL", experiment)
        started = time.monotonic()
        while (
            docker("inspect", interrupted[0]["Id"], check=False) != "[]"
            or docker("volume", "inspect", owned_volume, check=False) != "[]"
        ):
            if time.monotonic() - started > 25:
                raise TimeoutError("Interrupted sandbox or temporary volume was not removed")
            time.sleep(0.2)
        print(
            json.dumps(
                {"sandbox_cleanup": "passed", "seconds": round(time.monotonic() - started, 2)}
            ),
            flush=True,
        )
        docker("start", experiment)
        actor("uncertain")
    finally:
        for cid in reversed(containers):
            record = json.loads(docker("inspect", cid))[0]
            assert record["Config"]["Labels"].get("autoscholar.acceptance") == tag
            docker("rm", "--force", "--volumes", cid)
        for volume in reversed(volumes):
            record = json.loads(docker("volume", "inspect", volume))[0]
            assert record["Labels"].get("autoscholar.acceptance") == tag
            docker("volume", "rm", volume)
        if network_created:
            record = json.loads(docker("network", "inspect", tag))[0]
            assert record["Labels"].get("autoscholar.acceptance") == tag
            docker("network", "rm", tag)
        print(json.dumps({"cleanup": "passed", "isolated_stack": tag}), flush=True)


def origin() -> None:
    from autoscholar.tool_platform.git_manager import GitManager

    repo = Path("/git/fixture-origin")
    repo.mkdir()
    (repo / "model.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "test_model.py").write_text(
        "from model import value\n\ndef test_value():\n    assert value == 2\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("Public offline fixture\n", encoding="utf-8")
    for command in (
        ["init", str(repo)],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.org",
            "commit",
            "-m",
            "Fixture",
        ],
    ):
        subprocess.run(
            ["git", *command],
            cwd=repo,
            env=GitManager.environment(),
            check=True,
            capture_output=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--client", choices=["prepare", "resume", "receipt", "interrupt", "uncertain"]
    )
    parser.add_argument("--git-server", action="store_true")
    parser.add_argument("--origin", action="store_true")
    options = parser.parse_args()
    if options.origin:
        origin()
    elif options.git_server:
        git_serve()
    elif options.client:
        asyncio.run(client(options.client))
    else:
        orchestrate()
