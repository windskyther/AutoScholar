from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from autoscholar.core.budget import BudgetLimits
from autoscholar.core.config import Settings
from autoscholar.main import create_app
from autoscholar.rag.database_models import DocumentJobRow, DocumentRow
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.storage import LocalDocumentStorage
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

AUTH = {"Authorization": "Bearer public-workbench-write-test"}


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    knowledge = KnowledgeRepository(service.tasks.session_factory)
    project = await knowledge.create_project(name="文档项目", description=None)
    other = await knowledge.create_project(name="其他项目", description=None)
    for document_id, project_id, state in (
        ("ready", project.id, "ready"),
        ("failed", project.id, "failed"),
        ("foreign", other.id, "ready"),
    ):
        await knowledge.create_document(
            document_id=document_id,
            project_id=project_id,
            original_filename="public.pdf",
            title="Public fixture",
            content_type="application/pdf",
            storage_key=document_id + ".pdf",
            sha256=document_id.ljust(64, "0"),
            size_bytes=100,
        )
        async with service.tasks.session_factory() as session:
            row = await session.get(DocumentRow, document_id)
            assert row is not None
            row.status = state
            await session.commit()
    app = create_app(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            experiment_api_token=SecretStr("public-workbench-write-test"),
            document_max_bytes=1024,
            autonomous_budget=BudgetLimits(total_tokens=1000, model_calls=2),
        ),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=knowledge,
        document_storage=LocalDocumentStorage(tmp_path / "documents"),
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
        research_services=[],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.headers["X-Fixture-Project"] = project.id
            # Test-only repository reference, not exposed in an HTTP payload.
            client.fixture_sessions = service.tasks.session_factory  # type: ignore[attr-defined]
            yield client
    finally:
        try:
            assert not provider.calls and not sandbox.requests
        finally:
            await engine.dispose()


async def test_public_session_exposes_limits_not_credentials(api: httpx.AsyncClient) -> None:
    response = await api.get("/workbench/session", headers=AUTH)
    caps = response.json()["capabilities"]
    assert caps["document_max_bytes"] == 1024
    assert caps["budget_limits"]["total_tokens"] == 1000
    assert caps["budget_limits"]["model_calls"] == 2
    assert "public-workbench-write-test" not in response.text


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("POST", "/documents"),
        ("POST", "/documents/failed/retry"),
        ("POST", "/documents/ready/reindex"),
        ("DELETE", "/documents/ready"),
    ],
)
async def test_document_writes_require_bearer(
    api: httpx.AsyncClient, method: str, suffix: str
) -> None:
    project_id = api.headers["X-Fixture-Project"]
    assert (
        await api.request(method, f"/workbench/projects/{project_id}{suffix}")
    ).status_code == 401


async def test_pdf_upload_duplicate_size_and_format(api: httpx.AsyncClient) -> None:
    project_id = api.headers["X-Fixture-Project"]
    path = f"/workbench/projects/{project_id}/documents"
    files = {"file": ("public.pdf", b"%PDF-1.4\npublic fixture", "application/pdf")}
    uploaded = await api.post(path, files=files, data={"title": "公开 PDF"}, headers=AUTH)
    assert uploaded.status_code == 202
    assert uploaded.json()["status"] == "queued"
    assert uploaded.headers["location"].startswith("/workbench/projects/")
    assert (await api.post(path, files=files, headers=AUTH)).status_code == 409
    invalid = await api.post(
        path, files={"file": ("bad.pdf", b"not pdf", "application/pdf")}, headers=AUTH
    )
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_pdf"
    large = await api.post(
        path, files={"file": ("large.pdf", b"%PDF-" + b"x" * 1024, "application/pdf")}, headers=AUTH
    )
    assert large.status_code == 422 and large.json()["error"]["code"] == "document_too_large"
    foreign = await api.delete(f"{path}/foreign", headers=AUTH)
    assert foreign.status_code == 404


async def test_document_jobs_recheck_state_and_delete_deduplicates(api: httpx.AsyncClient) -> None:
    project_id = api.headers["X-Fixture-Project"]
    path = f"/workbench/projects/{project_id}/documents"
    assert (await api.post(f"{path}/failed/retry", headers=AUTH)).json()["status"] == "queued"
    assert (await api.post(f"{path}/failed/retry", headers=AUTH)).status_code == 409
    assert (await api.post(f"{path}/ready/reindex", headers=AUTH)).json()["status"] == "queued"
    assert (await api.post(f"{path}/ready/reindex", headers=AUTH)).status_code == 409
    for _ in range(2):
        assert (await api.delete(f"{path}/ready", headers=AUTH)).status_code == 202
    async with api.fixture_sessions() as session:  # type: ignore[attr-defined]
        count = await session.scalar(
            select(func.count())
            .select_from(DocumentJobRow)
            .where(
                DocumentJobRow.document_id == "ready",
                DocumentJobRow.kind == "delete",
            )
        )
    assert count == 1


@pytest.mark.parametrize("ids", [["foreign"], ["failed"], ["missing"], ["ready", "foreign"]])
async def test_submission_rejects_unavailable_documents(
    api: httpx.AsyncClient, ids: list[str]
) -> None:
    response = await api.post(
        "/workbench/agent/tasks",
        headers=AUTH | {"Idempotency-Key": "invalid-docs"},
        json={
            "objective": "公开测试",
            "mode": "autonomous",
            "project_id": api.headers["X-Fixture-Project"],
            "document_ids": ids,
        },
    )
    assert (
        response.status_code == 422
        and response.json()["error"]["code"] == "selected_documents_unavailable"
    )


@pytest.mark.parametrize("ids", [["ready", "ready"], ["x" * 65], [str(i) for i in range(101)]])
async def test_submission_bounds_document_selection(api: httpx.AsyncClient, ids: list[str]) -> None:
    response = await api.post(
        "/workbench/agent/tasks",
        headers=AUTH | {"Idempotency-Key": "bounded-docs"},
        json={
            "objective": "公开测试",
            "mode": "autonomous",
            "project_id": api.headers["X-Fixture-Project"],
            "document_ids": ids,
        },
    )
    assert response.status_code == 422


async def test_submission_replay_preserves_identity_and_budget_after_document_changes(
    api: httpx.AsyncClient,
) -> None:
    payload = {
        "objective": "公开验收目标",
        "mode": "autonomous",
        "project_id": api.headers["X-Fixture-Project"],
        "document_ids": ["ready"],
        "budget": {"total_tokens": 2000, "model_calls": 5},
    }
    headers = AUTH | {"Idempotency-Key": "original-browser-request"}
    submitted = await api.post("/workbench/agent/tasks", json=payload, headers=headers)
    assert submitted.status_code == 202
    task_id = submitted.json()["task_id"]
    assert submitted.json()["status"] == "queued"
    await api.delete(f"/workbench/projects/{payload['project_id']}/documents/ready", headers=AUTH)
    repeated = await api.post("/workbench/agent/tasks", json=payload, headers=headers)
    assert repeated.json()["task_id"] == task_id and repeated.json()["created"] is False
    conflict = await api.post(
        "/workbench/agent/tasks", json=payload | {"objective": "不同目标"}, headers=headers
    )
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    )
    overview = (await api.get(f"/workbench/tasks/{task_id}/overview", headers=AUTH)).json()
    assert overview["execution"]["status"] == "queued"
    assert overview["execution"]["budget_limits"]["total_tokens"] == 1000
    assert overview["execution"]["budget_limits"]["model_calls"] == 2
