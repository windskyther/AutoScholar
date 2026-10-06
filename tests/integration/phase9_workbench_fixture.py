"""Loopback-only, read-only HTTP fixture for browser/API connectivity, never real providers."""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import uvicorn
from fastapi import FastAPI, Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.responses import Response

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from autoscholar.agent.database_models import AgentTaskRow, Base  # noqa: E402
from autoscholar.agent.repository import AgentTaskRepository  # noqa: E402
from autoscholar.coding.workspace import WorkspaceManager  # noqa: E402
from autoscholar.core.config import Settings  # noqa: E402
from autoscholar.core.responses import UTF8JSONResponse  # noqa: E402
from autoscholar.orchestration.models import PlanStep, TaskPlan  # noqa: E402
from autoscholar.orchestration.repository import WorkflowRepository  # noqa: E402
from autoscholar.rag.database_models import ProjectRow  # noqa: E402
from autoscholar.rag.repository import KnowledgeRepository  # noqa: E402
from tests.test_agent_runner import ScriptedProvider  # noqa: E402
from tests.test_experiment_service import FakeExperimentSandbox  # noqa: E402

TOKEN = "public-test-fixture-token"


class InertDependency:
    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


async def fixture_app() -> FastAPI:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        llm_api_key=None,
        llm_model=None,
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
        workspace_root=ROOT / "data" / "phase9-fixture-unused",
        document_storage_path=ROOT / "data" / "phase9-fixture-unused-documents",
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
    application = create_app(
        settings,
        database=InertDependency(),
        redis=InertDependency(),
        qdrant=InertDependency(),
        llm_provider=provider,
        agent_repository=tasks,
        knowledge_repository=KnowledgeRepository(sessions),
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
        if request.method not in {"GET", "HEAD"}:
            return UTF8JSONResponse({"error": {"code": "fixture_read_only"}}, status_code=405)
        if request.url.path.startswith("/workbench/"):
            api_requests += 1
        return await call_next(request)

    @application.get("/__fixture/status")
    async def status() -> dict[str, int]:
        return {
            "model_calls": len(provider.calls),
            "sandbox_calls": len(sandbox.requests),
            "api_requests": api_requests,
        }

    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with original_lifespan(app):
                yield
        finally:
            await engine.dispose()
            assert not provider.calls and not sandbox.requests

    application.router.lifespan_context = lifespan
    return application


async def main() -> None:
    app = await fixture_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=18009,
            log_level="warning",
            access_log=False,
        )
    )
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
