from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from autoscholar.agent.database_models import ArtifactRow, EvidenceRow, ExperimentRow
from autoscholar.core.budget import current_parent
from autoscholar.core.config import Settings
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.main import create_app
from autoscholar.rag.repository import KnowledgeRepository
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

AUTH = {"Authorization": "Bearer public-resource-test-token"}
REPORT = "# 中文报告\n<script>alert('xss')</script>\n".encode()


@pytest.fixture
async def resources(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Any, str, str]]:
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    knowledge = KnowledgeRepository(service.tasks.session_factory)
    project = await knowledge.create_project(name="资源测试", description=None)
    other = await knowledge.create_project(name="其他项目", description=None)
    await service.tasks.create_task(
        task_id="root", objective="public", mode="autonomous", project_id=project.id
    )
    parent = current_parent.set("root")
    try:
        for task, project_id in (("child", project.id), ("foreign", other.id)):
            await service.tasks.create_task(
                task_id=task, objective="public", mode="experiment", project_id=project_id
            )
    finally:
        current_parent.reset(parent)
    manager = ArtifactManager(service.workspace, service.tasks)
    ids = []
    for task in ("child", "foreign"):
        service.workspace.initialize(task)
        experiment = await service.tasks.create_experiment(
            task_id=task,
            name="public-experiment",
            specification=ExperimentSpecification().model_dump(),
        )
        artifact = await manager.save(task, experiment.id, "reports/report.md", REPORT)
        ids.append(artifact.id)
        await service.tasks.add_evidence(
            task_id=task,
            citation_key="S1",
            source_type="web",
            provider="public",
            title="公开证据",
            url="javascript:alert(1)",
            authors=(),
            year=None,
            external_id=None,
            query="hidden query",
            topic="public",
            claim="C" * 9000,
            excerpt="public",
            relevance=0.5,
        )
        async with service.tasks.session_factory() as session:
            row = await session.get(ExperimentRow, experiment.id)
            assert row
            row.specification["api_key"] = "secret-not-exported"
            row.metrics = {
                "api_key": "secret-not-exported",
                "winner": "cnn",
                "cnn_minus_mlp_accuracy": 0.1,
            }
            await session.commit()
    app = create_app(
        Settings(_env_file=None, experiment_api_token=SecretStr("public-resource-test-token")),  # type: ignore[call-arg]
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=knowledge,
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
        research_services=[],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, service, ids[0], ids[1]
        assert not provider.calls and not sandbox.requests
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", ["evidence", "experiments", "artifacts"])
async def test_resources_scoped_paginated_and_authenticated(resources: Any, kind: str) -> None:
    client, _, _, _ = resources
    path = "/workbench/tasks/root/resources/" + kind
    assert (await client.get(path)).status_code == 401
    response = await client.get(path, headers=AUTH, params={"limit": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["task_id"] == "root" and data["total"] == 1
    assert data["items"][0]["task_id"] == "child"
    assert "secret-not-exported" not in response.text and "hidden query" not in response.text
    assert (await client.get(path, headers=AUTH, params={"offset": 1})).json()["items"] == []
    assert (await client.get(path, headers=AUTH, params={"limit": 101})).status_code == 422
    assert (await client.get(path.replace("/root/", "/child/"), headers=AUTH)).status_code == 404
    if kind == "evidence":
        assert data["items"][0]["url"] is None
        assert len(data["items"][0]["claim"]) == 8192 and data["items"][0]["text_truncated"]


async def test_verified_report_preview_and_export(resources: Any) -> None:
    client, _, artifact, _ = resources
    path = f"/workbench/tasks/root/resources/artifacts/{artifact}"
    view = (await client.get(path + "/preview", headers=AUTH)).json()
    assert view["text"] == REPORT.decode() and view["artifact_id"] == artifact
    result = await client.get(path + "/content", headers=AUTH)
    assert result.content == REPORT
    assert result.headers["x-content-sha256"] == view["sha256"]
    assert result.headers["content-type"].startswith("text/markdown")
    assert result.headers["content-disposition"].startswith("attachment")
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("suffix", ["preview", "content"])
async def test_foreign_or_missing_artifact_not_exported(resources: Any, suffix: str) -> None:
    client, _, artifact, foreign = resources
    for owner, identifier in (("root", foreign), ("root", "missing"), ("child", artifact)):
        response = await client.get(
            f"/workbench/tasks/{owner}/resources/artifacts/{identifier}/{suffix}", headers=AUTH
        )
        assert response.status_code == 404
    assert (
        await client.get(f"/workbench/tasks/root/resources/artifacts/{artifact}/{suffix}")
    ).status_code == 401


@pytest.mark.parametrize("suffix", ["preview", "content"])
async def test_modified_file_rejected(resources: Any, suffix: str) -> None:
    client, service, artifact, _ = resources
    service.workspace.write_bytes("child", "report.md", b"tampered", area="reports", overwrite=True)
    result = await client.get(
        f"/workbench/tasks/root/resources/artifacts/{artifact}/{suffix}", headers=AUTH
    )
    assert (
        result.status_code == 409 and result.json()["error"]["code"] == "artifact_integrity_failed"
    )


@pytest.mark.parametrize(
    "path", [".env", "reports/../.env", "reports/design.md", "reports/report.html"]
)
async def test_unregistered_private_paths_are_hidden(resources: Any, path: str) -> None:
    client, service, artifact, _ = resources
    async with service.tasks.session_factory() as session:
        row = await session.get(ArtifactRow, artifact)
        assert row
        row.path = path
        await session.commit()
    listing = (await client.get("/workbench/tasks/root/resources/artifacts", headers=AUTH)).json()
    assert listing["total"] == 0
    response = await client.get(
        f"/workbench/tasks/root/resources/artifacts/{artifact}/content", headers=AUTH
    )
    assert response.status_code == 404


async def test_preview_truncation_preserves_chinese_and_manifest(resources: Any) -> None:
    client, service, artifact, _ = resources
    record = await service.tasks.get_artifact(artifact)
    assert record
    content = ("中" * 90000).encode()
    # Re-registered output is independently verified; no arbitrary file reads.
    service.workspace.initialize("root")
    experiment = await service.tasks.create_experiment(
        task_id="root", name="bounded", specification={}
    )
    updated = await ArtifactManager(service.workspace, service.tasks).save(
        "root", experiment.id, record.path, content
    )
    response = await client.get(
        f"/workbench/tasks/root/resources/artifacts/{updated.id}/preview", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["truncated"] and len(response.json()["text"].encode()) <= 262144


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/paper",
        "https://user:password@example.org",
        "data:text/html,x",
        "https://example.org/\n",
    ],
)
async def test_external_url_projection(resources: Any, url: str) -> None:
    client, service, _, _ = resources
    from sqlalchemy import select

    async with service.tasks.session_factory() as session:
        row = await session.scalar(select(EvidenceRow).where(EvidenceRow.task_id == "child"))
        assert row
        row.url = url
        await session.commit()
    item = (await client.get("/workbench/tasks/root/resources/evidence", headers=AUTH)).json()[
        "items"
    ][0]
    assert item["url"] == (url if url == "https://example.org/paper" else None)
