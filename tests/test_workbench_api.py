from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.core.budget import current_parent
from autoscholar.core.config import Settings
from autoscholar.main import create_app
from autoscholar.orchestration.durable import DurableService
from autoscholar.rag.repository import KnowledgeRepository
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

TOKEN = "phase9-test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    knowledge = KnowledgeRepository(service.tasks.session_factory)
    project = await knowledge.create_project(name="项目 A", description=None)
    other = await knowledge.create_project(name="项目 B", description=None)
    await service.tasks.create_task(
        task_id="root", objective="MNIST 100% 测试", project_id=project.id, mode="autonomous"
    )
    await service.tasks.create_task(
        task_id="second", objective="第二个任务", project_id=project.id, mode="research"
    )
    parent = current_parent.set("root")
    try:
        await service.tasks.create_task(
            task_id="child", objective="子任务", project_id=project.id, mode="research"
        )
        # Deliberately corrupt ancestry across projects; the facade must not follow it.
        await service.tasks.create_task(
            task_id="foreign", objective="其他项目的私有内容", project_id=other.id, mode="research"
        )
    finally:
        current_parent.reset(parent)
    for task_id in ("child", "foreign"):
        await service.tasks.add_evidence(
            task_id=task_id,
            citation_key="S1",
            source_type="web",
            provider="tavily",
            title=task_id,
            url="https://example.org/public",
            authors=(),
            year=None,
            external_id=None,
            query="MNIST",
            topic="fixture",
            claim="公开资料",
            excerpt="仅测试, 无外部请求",
            relevance=0.8,
        )
        experiment = await service.tasks.create_experiment(
            task_id=task_id, name="fixture", specification={}
        )
        await service.tasks.add_artifact(
            task_id=task_id,
            experiment_id=experiment.id,
            artifact_type="report",
            path="reports/report.md",
            media_type="text/markdown",
            size_bytes=1,
            sha256="0" * 64,
        )
    async with service.tasks.session_factory() as session:
        row = await session.get(AgentTaskRow, "root")
        assert row is not None
        row.metrics = {"total_tokens": 12, "provider_secret": 999}
        row.answer = "x" * 262145
        child = await session.get(AgentTaskRow, "child")
        assert child is not None
        child.metrics = {"total_tokens": 12}
        await session.commit()
    app = create_app(
        Settings(
            experiment_api_token=SecretStr(TOKEN),
            llm_api_key=SecretStr("hidden-llm-key"),
            llm_model="test-model",
        ),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=knowledge,
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
    )
    app.state.test_project_id = project.id
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Test-only context without placing credentials in URLs.
            client.headers["X-Fixture-Project"] = project.id
            yield client
    finally:
        try:
            assert not provider.calls, "Read-only workbench called the model"
        finally:
            await engine.dispose()


@pytest.mark.parametrize(
    "path",
    [
        "/session",
        "/projects",
        "/tasks/root/overview",
        "/tasks/root/evidence",
        "/tasks/root/experiments",
        "/tasks/root/artifacts",
        "/health/live",
        "/agent/tasks/root",
        "/projects/missing/documents",
        "/projects/missing/memory",
    ],
)
async def test_workbench_all_reads_require_token(api: httpx.AsyncClient, path: str) -> None:
    assert (await api.get("/workbench" + path)).status_code == 401
    assert (
        await api.get("/workbench" + path, headers={"Authorization": "Bearer wrong"})
    ).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/projects"),
        ("POST", "/agent/run"),
        ("POST", "/agent/tasks"),
        ("POST", "/agent/tasks/root/resume"),
        ("PUT", "/projects/missing/memory"),
        ("DELETE", "/projects/missing/documents/missing"),
    ],
)
async def test_workbench_writes_require_token(
    api: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await api.request(method, "/workbench" + path, json={})
    assert response.status_code == 401


async def test_session_has_only_public_configuration(api: httpx.AsyncClient) -> None:
    response = await api.get("/workbench/session", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["capabilities"]["llm_configured"] is True
    assert response.json()["capabilities"]["task_streaming"] is False
    assert "hidden-llm-key" not in response.text and TOKEN not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_project_tasks_filter_page_and_exclude_children(api: httpx.AsyncClient) -> None:
    project_id = api.headers["X-Fixture-Project"]
    path = f"/workbench/projects/{project_id}/tasks"
    response = await api.get(path, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {r["task_id"] for r in response.json()["items"]} == {"root", "second"}
    response = await api.get(path, params={"mode": "autonomous", "q": "100%"}, headers=AUTH)
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["task_id"] == "root"
    response = await api.get(path, params={"q": "_"}, headers=AUTH)
    assert response.json()["total"] == 0
    response = await api.get(path, params={"limit": 1, "offset": 1}, headers=AUTH)
    assert len(response.json()["items"]) == 1 and response.json()["total"] == 2
    response = await api.get(path, params={"status": "failed"}, headers=AUTH)
    assert response.json()["total"] == 0
    for params in ({"limit": 101}, {"offset": -1}, {"status": "invented"}, {"q": "a" * 201}):
        assert (await api.get(path, params=params, headers=AUTH)).status_code == 422
    assert (await api.get("/workbench/projects/missing/tasks", headers=AUTH)).status_code == 404


async def test_overview_bounded_same_project_usage_not_double_counted(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/workbench/tasks/root/overview", headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()
    assert [r["task_id"] for r in data["children"]["items"]] == ["child"]
    assert data["resources"] == {"evidence": 1, "experiments": 1, "artifacts": 1}
    assert data["task"]["metrics"]["total_tokens"] == 12
    assert data["usage_scope"] == "root_task_only" and data["monetary_cost"] is None
    assert "provider_secret" not in response.text
    assert data["answer_truncated"] and len(data["answer"]) == 262144
    assert data["execution"] is None and data["current_plan"] is None
    page = await api.get("/workbench/tasks/root/overview?offset=1", headers=AUTH)
    assert not page.json()["children"]["items"] and page.json()["children"]["total"] == 1
    for task_id in ("child", "missing"):
        assert (
            await api.get(f"/workbench/tasks/{task_id}/overview", headers=AUTH)
        ).status_code == 404


@pytest.mark.parametrize("kind", ["evidence", "experiments", "artifacts"])
async def test_family_resources_keep_task_identity_and_project_scope(
    api: httpx.AsyncClient, kind: str
) -> None:
    path = f"/workbench/tasks/root/{kind}"
    data = (await api.get(path, headers=AUTH)).json()
    assert data["total"] == 1
    assert data["items"][0]["task_id"] == "child"
    page = (await api.get(path, params={"offset": 1}, headers=AUTH)).json()
    assert page["items"] == [] and page["total"] == 1
    assert (await api.get(path, params={"limit": 101}, headers=AUTH)).status_code == 422


async def test_facade_existing_projects_and_durable_routes(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    knowledge = KnowledgeRepository(service.tasks.session_factory)
    app = create_app(
        Settings(experiment_api_token=SecretStr(TOKEN)),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=knowledge,
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
    )
    app.state.durable_service = DurableService(service, approval_threshold=0)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            project = await client.post(
                "/workbench/projects", json={"name": "中文项目"}, headers=AUTH
            )
            assert project.status_code == 201
            payload = {
                "objective": "MNIST 验收",
                "mode": "autonomous",
                "project_id": project.json()["id"],
            }
            response = await client.post(
                "/workbench/agent/tasks",
                json=payload,
                headers=AUTH | {"Idempotency-Key": "browser-fixture"},
            )
            assert response.status_code == 202, response.text
            task_id = response.json()["task_id"]
            assert response.json()["status_url"] == f"/workbench/agent/tasks/{task_id}"
            repeated = await client.post(
                "/workbench/agent/tasks",
                json=payload,
                headers=AUTH | {"Idempotency-Key": "browser-fixture"},
            )
            assert repeated.json()["task_id"] == task_id and not repeated.json()["created"]
            overview = (
                await client.get(f"/workbench/tasks/{task_id}/overview", headers=AUTH)
            ).json()
            assert overview["execution"]["status"] == "queued"
            assert overview["execution"]["budget_limits"]["total_tokens"] == 120000
            await app.state.durable_service.tick()
            overview = (
                await client.get(f"/workbench/tasks/{task_id}/overview", headers=AUTH)
            ).json()
            assert overview["current_plan"]["version"] == 1
            assert overview["current_plan"]["plan"]["steps"]
            assert (
                "owner" not in overview["execution"]
                and "pending_calls" not in overview["execution"]
            )
            # Existing callers remain compatible; no implicit global auth migration.
            assert (await client.get("/projects")).status_code == 200
    finally:
        await engine.dispose()
