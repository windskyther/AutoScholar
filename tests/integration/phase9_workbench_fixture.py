"""Loopback-only HTTP fixture with optional isolated writes, never real providers."""

import asyncio
import io
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from pydantic import SecretStr
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from autoscholar.agent.database_models import AgentTaskRow, Base  # noqa: E402
from autoscholar.agent.repository import AgentTaskRepository  # noqa: E402
from autoscholar.coding.workspace import WorkspaceManager  # noqa: E402
from autoscholar.core.budget import current_parent  # noqa: E402
from autoscholar.core.config import Settings  # noqa: E402
from autoscholar.core.errors import AppError  # noqa: E402
from autoscholar.core.responses import UTF8JSONResponse  # noqa: E402
from autoscholar.experiment.artifacts import ArtifactManager  # noqa: E402
from autoscholar.experiment.models import ExperimentSpecification  # noqa: E402
from autoscholar.orchestration.checkpoints import digest  # noqa: E402
from autoscholar.orchestration.durable_models import (  # noqa: E402
    WorkflowApprovalRow,
    WorkflowCheckpointRow,
    WorkflowJobRow,
)
from autoscholar.orchestration.models import PlanStep, TaskPlan  # noqa: E402
from autoscholar.orchestration.repository import WorkflowRepository  # noqa: E402
from autoscholar.rag.chunking import StructureAwareChunker  # noqa: E402
from autoscholar.rag.database_models import ProjectRow  # noqa: E402
from autoscholar.rag.embeddings import EmbeddingProvider  # noqa: E402
from autoscholar.rag.index import ChunkIndex  # noqa: E402
from autoscholar.rag.parser import PDFParser  # noqa: E402
from autoscholar.rag.repository import KnowledgeRepository  # noqa: E402
from autoscholar.rag.storage import LocalDocumentStorage  # noqa: E402
from autoscholar.rag.worker import DocumentWorker  # noqa: E402
from tests.test_agent_runner import ScriptedProvider  # noqa: E402
from tests.test_experiment_service import FakeExperimentSandbox  # noqa: E402
from tests.test_rag_worker import FakeEmbeddings, FakeIndex  # noqa: E402

TOKEN = "public-test-fixture-token"


class InertDependency:
    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def public_pdf(*, blank: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    if not blank:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): writer._add_object(font),
                    }
                )
            }
        )
        content = DecodedStreamObject()
        text = b"Public MNIST browser acceptance fixture with searchable scientific text. " * 6
        content.set_data(b"BT /F1 12 Tf 20 100 Td (" + text + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def fixture_app(
    *, writes: bool = False, streams: bool = False, controls: bool = False, resources: bool = False
) -> FastAPI:
    data_root = (ROOT / "data" / "validation").resolve()
    assert data_root.is_relative_to(ROOT.resolve())
    temporary = None
    if writes or streams or controls or resources:
        data_root.mkdir(parents=True, exist_ok=True)
        prefix = (
            "phase9f-"
            if resources
            else "phase9e-"
            if controls
            else "phase9d-"
            if streams
            else "phase9c-"
        )
        temporary = TemporaryDirectory(prefix=prefix, dir=data_root)
    temporary_root = Path(temporary.name) if temporary else ROOT / "data" / "phase9-fixture-unused"
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        log_level="WARNING",
        llm_api_key=SecretStr("public-fixture-never-used") if writes else None,
        llm_model="fixture-never-called" if writes else None,
        tavily_api_key=None,
        semantic_scholar_api_key=None,
        embedding_api_key=None,
        qdrant_api_key=None,
        mcp_service_token=None,
        research_tool_backend="native",
        filesystem_tool_backend="native",
        git_tool_backend="disabled",
        experiment_tool_backend="native",
        experiment_api_token=SecretStr(TOKEN),
        workspace_root=temporary_root / "workspaces",
        document_storage_path=temporary_root / "documents",
        document_max_bytes=2048 if writes else 52428800,
    )
    # main also constructs a module-level app: prevent that import from loading .env.
    with patch("autoscholar.core.config.get_settings", return_value=settings):
        from autoscholar.main import create_app

    # Concurrent polling/SSE and write transactions must not share the one
    # StaticPool connection of in-memory SQLite: a reader rollback can undo a
    # writer. File-backed SQLite gives each session its own transaction.
    database_url = (
        f"sqlite+aiosqlite:///{temporary_root / 'fixture.sqlite'}"
        if temporary is not None
        else "sqlite+aiosqlite:///:memory:"
    )
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tasks = AgentTaskRepository(sessions)
    async with sessions() as session:
        session.add(ProjectRow(id="project-a", name="中文研究项目", description="隔离 API 联测"))
        await session.commit()
    await tasks.create_task(
        task_id="task-a", objective="比较 MNIST 模型", project_id="project-a", mode="autonomous"
    )
    async with sessions() as session:
        row = await session.get(AgentTaskRow, "task-a")
        assert row is not None
        row.status = "paused"
        row.answer = "中文输出：隔离 HTTP 联测，不调用付费接口。"  # noqa: RUF001
        row.metrics = {"total_tokens": 12}
        await session.commit()
    await WorkflowRepository(sessions).save_plan(
        "task-a",
        1,
        TaskPlan(
            goal="比较 MNIST 模型",
            steps=[
                PlanStep(
                    id="research",
                    type="research",
                    description="查找公开资料",
                    expected_output="证据",
                )
            ],
        ),
        "public browser fixture",
    )
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    knowledge = KnowledgeRepository(sessions)
    storage = LocalDocumentStorage(settings.document_storage_path)
    index = FakeIndex()
    worker = DocumentWorker(
        repository=knowledge,
        storage=storage,
        parser=PDFParser(),
        chunker=StructureAwareChunker(max_tokens=32, overlap_tokens=4),
        embeddings=cast(EmbeddingProvider, FakeEmbeddings()),
        index=cast(ChunkIndex, index),
        lease_seconds=30,
        max_attempts=1,
        parse_timeout_seconds=5,
        max_pages=10,
    )
    application = create_app(
        settings,
        database=InertDependency(),
        redis=InertDependency(),
        qdrant=InertDependency(),
        llm_provider=provider,
        agent_repository=tasks,
        knowledge_repository=knowledge,
        document_storage=storage,
        workspace_manager=WorkspaceManager(settings.workspace_root),
        sandbox_executor=sandbox,
        research_services=[],
    )
    if resources:
        parent = current_parent.set("task-a")
        try:
            await tasks.create_task(
                task_id="experiment-child",
                objective="public",
                mode="experiment",
                project_id="project-a",
            )
        finally:
            current_parent.reset(parent)
        workspace = application.state.workspace_manager
        workspace.initialize("experiment-child")
        experiment = await tasks.create_experiment(
            task_id="experiment-child",
            name="public-browser-mnist",
            specification=ExperimentSpecification().model_dump(),
        )
        manager = ArtifactManager(workspace, tasks)
        await manager.save(
            "experiment-child",
            experiment.id,
            "reports/report.md",
            "# 中文公开报告\n<script>window.resourceXSS = true</script>\n仅限工程验收。".encode(),
        )
        await manager.save(
            "experiment-child", experiment.id, "outputs/metrics.json", b'{"public": true}'
        )
        await tasks.add_evidence(
            task_id="experiment-child",
            citation_key="S1",
            source_type="web",
            provider="fixture",
            title="公开 MNIST 证据",
            url="https://example.org/paper",
            authors=("Public Author",),
            year=2026,
            external_id=None,
            query="public",
            topic="MNIST",
            claim="公开测试主张",
            excerpt="<script>window.resourceXSS = true</script>",
            relevance=0.8,
        )
    stream_task = ""
    control_task = ""
    stream_patch = None
    if streams:
        from autoscholar.api.routes import workbench as workbench_routes
        from autoscholar.api.workbench_events import event_stream as native_stream

        stream_patch = patch.object(
            workbench_routes,
            "event_stream",
            partial(
                native_stream,
                poll_seconds=0.05,
                heartbeat_seconds=0.2,
                duration_seconds=2,
            ),
        )
        stream_patch.start()
        durable = application.state.durable_service
        stream_task, _ = await durable.submit(
            {"objective": "公开的实时事件验收", "mode": "autonomous", "project_id": "project-a"},
            "public-stream-fixture",
        )
        async with sessions() as session:
            for _ in range(104):
                await durable.repository.event(session, stream_task, "budget_saved")
            await session.commit()

    async def seed_control() -> str:
        nonlocal control_task
        durable = application.state.durable_service
        control_task, _ = await durable.submit(
            {"objective": "公开审批验收任务", "mode": "autonomous", "project_id": "project-a"},
            "public-control-fixture-" + str(uuid4()),
        )
        snapshot, _ = await durable.repository.snapshot(control_task)
        snapshot.version = 1
        snapshot.stage = "executor"
        snapshot.plan = TaskPlan(
            goal=snapshot.objective,
            steps=[
                PlanStep(
                    id="code", type="coding", description="公开夹具代码", expected_output="train.py"
                ),
                PlanStep(
                    id="train",
                    type="experiment",
                    description="公开夹具实验",
                    expected_output="metrics",
                    dependencies=["code"],
                ),
            ],
        )
        await WorkflowRepository(sessions).save_plan(
            control_task, 1, snapshot.plan, "public control fixture"
        )
        operation = {
            "task_id": control_task,
            "plan_version": 1,
            "step_id": "train",
            "operation": "experiment",
            "risk_level": 3,
            "specification": snapshot.specification.model_dump(),
            "budget_limits": snapshot.limits.model_dump(),
            "source_sha256": "0" * 64,
            "cost_units": snapshot.specification.epochs * snapshot.specification.train_samples * 2,
        }
        async with sessions() as session:
            job = await durable.repository.locked(session, control_task)
            durable.repository.checkpoint(session, job, snapshot)
            await durable.repository.status(session, job, "awaiting_approval")
            session.add(
                WorkflowApprovalRow(
                    id="approval-" + control_task,
                    task_id=control_task,
                    operation_sha256=digest(operation),
                    payload=operation,
                    status="pending",
                    reason="公开实验审批夹具，不会实际执行训练",  # noqa: RUF001
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
            )
            await durable.repository.event(session, control_task, "approval_requested")
            await session.commit()
        return cast(str, control_task)

    if controls:
        await seed_control()
    api_requests = 0

    @application.middleware("http")
    async def read_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonlocal api_requests
        allowed_write = writes and (
            request.url.path
            in {"/workbench/projects", "/workbench/agent/tasks", "/__fixture/process"}
            or re.fullmatch(
                r"/workbench/projects/[^/]+/documents(?:/[^/]+(?:/(?:retry|reindex))?)?",
                request.url.path,
            )
        )
        allowed_write = allowed_write or (streams and request.url.path == "/__fixture/advance")
        allowed_write = allowed_write or (
            controls
            and bool(
                re.fullmatch(
                    r"/workbench/tasks/[^/]+/(?:control/(?:pause|resume|cancel)|approvals/[^/]+/decision)",
                    request.url.path,
                )
            )
        )
        allowed_write = allowed_write or (
            controls and request.url.path == "/__fixture/control-seed"
        )
        if (
            request.method not in {"GET", "HEAD"}
            and not allowed_write
            and request.url.path != "/__fixture/stop"
        ):
            return UTF8JSONResponse({"error": {"code": "fixture_read_only"}}, status_code=405)
        if request.url.path.startswith("/workbench/"):
            api_requests += 1
        return await call_next(request)

    @application.get("/__fixture/status")
    async def status() -> dict[str, int | str]:
        async with sessions() as session:
            jobs = await session.scalar(select(func.count()).select_from(WorkflowJobRow))
        return {
            "model_calls": len(provider.calls),
            "sandbox_calls": len(sandbox.requests),
            "api_requests": api_requests,
            "task_jobs": int(jobs or 0),
            "indexed_chunks": index.indexed,
            "stream_task_id": stream_task,
            "control_task_id": control_task,
        }

    def fixture_authorization(request: Request) -> None:
        if request.headers.get("Authorization") != "Bearer " + TOKEN:
            raise AppError(
                status_code=401, code="fixture_auth_required", message="Fixture token required"
            )

    @application.post("/__fixture/control-seed")
    async def control_seed(request: Request) -> dict[str, str]:
        fixture_authorization(request)
        if not controls:
            raise AppError(status_code=404, code="fixture_read_only", message="Controls disabled")
        return {"task_id": await seed_control()}

    @application.get("/__fixture/pdf")
    async def pdf(blank: bool = False) -> Response:
        return Response(public_pdf(blank=blank), media_type="application/pdf")

    @application.post("/__fixture/process")
    async def process(request: Request) -> dict[str, bool]:
        fixture_authorization(request)
        return {"handled": await worker.run_once()}

    @application.post("/__fixture/advance")
    async def advance(request: Request, finish: bool = False) -> dict[str, int]:
        fixture_authorization(request)
        assert streams and stream_task
        async with sessions() as session:
            task = await session.get(AgentTaskRow, stream_task)
            job = await session.get(WorkflowJobRow, stream_task)
            assert task is not None and job is not None
            checkpoint = await session.scalar(
                select(WorkflowCheckpointRow).where(
                    WorkflowCheckpointRow.task_id == stream_task,
                    WorkflowCheckpointRow.sequence == job.checkpoint_sequence,
                )
            )
            assert task is not None and job is not None and checkpoint is not None
            task.status = job.status = "succeeded" if finish else "running"
            job.usage = {"total_tokens": 24, "model_calls": 1}
            stage = "done" if finish else "writer"
            checkpoint.payload = dict(checkpoint.payload) | {"stage": stage}
            checkpoint.sha256 = digest(checkpoint.payload)
            if finish:
                task.answer = "中文事件验收完成：仅夹具状态变化，没有执行模型。"  # noqa: RUF001
            await application.state.durable_service.repository.event(
                session,
                stream_task,
                "checkpoint_saved",
                stage=stage,
                status=task.status,
                sequence=job.checkpoint_sequence,
                reason="PRIVATE_FIXTURE_PARAMETER",
            )
            await session.commit()
            await session.refresh(task)
            return {"sequence": task.event_sequence}

    @application.post("/__fixture/stop")
    async def stop(request: Request) -> dict[str, bool]:
        fixture_authorization(request)
        application.state.fixture_server.should_exit = True
        return {"stopping": True}

    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with original_lifespan(app):
                yield
        finally:
            try:
                await engine.dispose()
            finally:
                if stream_patch is not None:
                    stream_patch.stop()
                if temporary is not None:
                    assert temporary_root.resolve().parent == data_root
                    assert temporary_root.name.startswith(
                        ("phase9c-", "phase9d-", "phase9e-", "phase9f-")
                    )
                    temporary.cleanup()
            assert not provider.calls and not sandbox.requests
            if temporary is not None:
                print("fixture_cleanup: passed; external_api_calls: 0", flush=True)

    application.router.lifespan_context = lifespan
    return application


async def main() -> None:
    app = await fixture_app(
        writes="--writes" in sys.argv[1:],
        streams="--streams" in sys.argv[1:],
        controls="--controls" in sys.argv[1:],
        resources="--resources" in sys.argv[1:],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=18009,
            log_level="warning",
            access_log=False,
        )
    )
    app.state.fixture_server = server
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
