from pathlib import Path

import httpx
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.config import Settings
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.main import create_app
from autoscholar.rag import database_models as rag_database_models  # noqa: F401
from tests.test_health import FakeDependency, FakeLLMProvider, FakeSandbox


async def test_experiment_artifact_api_requires_token_and_task_scope(tmp_path: Path) -> None:
    database_path = tmp_path / "experiment-api.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
    workspace = WorkspaceManager(tmp_path / "workspaces")
    for task_id in ("task-a", "task-b"):
        await repository.create_task(task_id=task_id, objective="Compare models")
        await repository.update_task(
            task_id,
            status="succeeded",
            plan=["Compare"],
            answer="Done",
            metrics={},
            mode="experiment",
        )
        workspace.initialize(task_id)
    await repository.create_task(
        task_id="running-task", objective="Running comparison", mode="experiment"
    )
    experiment = await repository.create_experiment(
        task_id="task-a", name="comparison", specification={"dataset": "mnist"}
    )
    artifact_manager = ArtifactManager(workspace, repository)
    artifact = await artifact_manager.save(
        "task-a", experiment.id, "reports/report.md", b"# Result\n"
    )
    app = create_app(
        Settings(experiment_api_token=SecretStr("long-test-token")),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=FakeLLMProvider(),
        agent_repository=repository,
        workspace_manager=workspace,
        artifact_manager=artifact_manager,
        sandbox_executor=FakeSandbox(),
    )
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": "Bearer long-test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        target = f"/agent/tasks/task-a/artifacts/{artifact.id}"
        assert (await client.get(target)).status_code == 401
        assert (await client.get(target, headers=auth)).content == b"# Result\n"
        assert (
            await client.get(f"/agent/tasks/task-b/artifacts/{artifact.id}", headers=auth)
        ).status_code == 404
        listed = await client.get("/agent/tasks/task-a/artifacts", headers=auth)
        assert listed.json()["items"][0]["sha256"] == artifact.sha256
        experiments = await client.get("/agent/tasks/task-a/experiments", headers=auth)
        assert experiments.json()["items"][0]["id"] == experiment.id
        task = await client.get("/agent/tasks/task-a")
        assert task.status_code == 401
        assert (await client.get("/agent/tasks/task-a", headers=auth)).status_code == 200
        assert (await client.get("/agent/tasks/running-task")).status_code == 401
        assert (await client.get("/agent/tasks/running-task/evidence")).status_code == 401
    await engine.dispose()
