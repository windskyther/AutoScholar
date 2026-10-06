"""Loopback-only HTTP fixture with optional isolated writes, never real providers."""

import asyncio
import io
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

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
from autoscholar.core.config import Settings  # noqa: E402
from autoscholar.core.errors import AppError  # noqa: E402
from autoscholar.core.responses import UTF8JSONResponse  # noqa: E402
from autoscholar.orchestration.durable_models import WorkflowJobRow  # noqa: E402
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


async def fixture_app(*, writes: bool = False) -> FastAPI:
    data_root = (ROOT / "data" / "validation").resolve()
    assert data_root.is_relative_to(ROOT.resolve())
    temporary = None
    if writes:
        data_root.mkdir(parents=True, exist_ok=True)
        temporary = TemporaryDirectory(prefix="phase9c-", dir=data_root)
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

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
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
    async def status() -> dict[str, int]:
        async with sessions() as session:
            jobs = await session.scalar(select(func.count()).select_from(WorkflowJobRow))
        return {
            "model_calls": len(provider.calls),
            "sandbox_calls": len(sandbox.requests),
            "api_requests": api_requests,
            "task_jobs": int(jobs or 0),
            "indexed_chunks": index.indexed,
        }

    def fixture_authorization(request: Request) -> None:
        if request.headers.get("Authorization") != "Bearer " + TOKEN:
            raise AppError(
                status_code=401, code="fixture_auth_required", message="Fixture token required"
            )

    @application.get("/__fixture/pdf")
    async def pdf(blank: bool = False) -> Response:
        return Response(public_pdf(blank=blank), media_type="application/pdf")

    @application.post("/__fixture/process")
    async def process(request: Request) -> dict[str, bool]:
        fixture_authorization(request)
        return {"handled": await worker.run_once()}

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
                if temporary is not None:
                    assert temporary_root.resolve().parent == data_root
                    assert temporary_root.name.startswith("phase9c-")
                    temporary.cleanup()
            assert not provider.calls and not sandbox.requests
            if temporary is not None:
                print("fixture_cleanup: passed; external_api_calls: 0", flush=True)

    application.router.lifespan_context = lifespan
    return application


async def main() -> None:
    app = await fixture_app(writes="--writes" in sys.argv[1:])
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
