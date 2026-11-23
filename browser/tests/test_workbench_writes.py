from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.core.budget import BudgetLimits
from autoscholar.core.config import Settings
from autoscholar.llm.profiles import ModelProfileCreate
from autoscholar.orchestration.checkpoints import Snapshot
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow
from autoscholar.rag.database_models import DocumentJobRow, DocumentRow
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.storage import LocalDocumentStorage
from tests.governance import offline_app
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
    app = offline_app(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            model_encryption_key=SecretStr("public-workbench-write-test"),
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
            client.headers["X-Fixture-Other-Project"] = other.id
            # Test-only repository reference, not exposed in an HTTP payload.
            client.fixture_sessions = service.tasks.session_factory  # type: ignore[attr-defined]
            profile = await app.state.model_profiles.create(
                ModelProfileCreate(
                    name="Managed fixture",
                    base_url="https://fixture.example/v1",
                    model="fixture",
                    api_key=SecretStr("public-fixture-key"),
                )
            )
            client.headers["X-Fixture-Model"] = profile.id
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
        await api.request(
            method,
            f"/workbench/projects/{project_id}{suffix}",
            headers={"Origin": "https://foreign.invalid"},
        )
    ).status_code == 403


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
            "model_profile_id": api.headers["X-Fixture-Model"],
            "workflow_kind": "research",
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
            "model_profile_id": api.headers["X-Fixture-Model"],
            "workflow_kind": "research",
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
        "model_profile_id": api.headers["X-Fixture-Model"],
        "workflow_kind": "research",
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


async def test_task_references_and_followups_preserve_each_round(api: httpx.AsyncClient) -> None:
    project = api.headers["X-Fixture-Project"]
    project_path = f"/workbench/projects/{project}"
    payload = {
        "model_profile_id": api.headers["X-Fixture-Model"],
        "workflow_kind": "research",
        "mode": "autonomous",
        "project_id": project,
    }

    async def submit(objective: str, **overrides: object) -> httpx.Response:
        return await api.post(
            "/workbench/agent/tasks",
            json=payload | {"objective": objective} | overrides,
            headers=AUTH | {"Idempotency-Key": objective.replace(" ", "-")},
        )

    async def create(objective: str, **overrides: object) -> str:
        response = await submit(objective, **overrides)
        assert response.status_code == 202, response.text
        return str(response.json()["task_id"])

    original = await create("Original report")
    reference = await create("Reference report")
    active = await create("Active report")
    foreign = await create("Other report", project_id=api.headers["X-Fixture-Other-Project"])
    long_answer = "Original text." + "x" * 4100 + "Final paragraph."
    async with api.fixture_sessions() as session:  # type: ignore[attr-defined]
        for task_id, answer in (
            (original, long_answer),
            (reference, "Selected reference result."),
            (foreign, "Foreign result."),
        ):
            row = await session.get(AgentTaskRow, task_id)
            assert row is not None
            row.status = "succeeded"
            row.answer = answer
        await session.commit()

    available = await api.get(f"{project_path}/reference-tasks", params={"q": "report"})
    assert {item["task_id"] for item in available.json()} == {original, reference}
    links_path = f"/workbench/tasks/{original}/links"
    for ids in ([original], [foreign], [active], [reference, reference]):
        rejected = await api.post(links_path, json={"task_ids": ids})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "task_reference_unavailable"
    rejected = await submit("Invalid follow-up", continuation_of_task_id=active)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "task_continuation_unavailable"
    saved = await api.post(links_path, json={"task_ids": [reference]})
    assert saved.status_code == 200 and saved.json()[0]["task_id"] == reference
    links = (await api.get(links_path)).json()
    assert links["can_append"] and links["model_profile_id"] == payload["model_profile_id"]
    assert links["workflow_kind"] == "research" and links["references"] == saved.json()

    followup = await submit(
        "Expand the comparison",
        continuation_of_task_id=original,
        reference_task_ids=[reference],
        budget={"total_tokens": 500},
    )
    assert followup.status_code == 202, followup.text
    followup_id = followup.json()["task_id"]
    followup_path = f"/workbench/tasks/{followup_id}/links"
    forward = (await api.get(links_path)).json()["followups"]
    assert forward["total"] == 1 and forward["items"][0]["task_id"] == followup_id
    reverse = (await api.get(followup_path)).json()
    assert reverse["previous"]["task_id"] == original and not reverse["can_append"]
    project_tasks = (await api.get(f"{project_path}/tasks")).json()
    assert {item["task_id"] for item in project_tasks["items"]} == {original, reference, active}

    assert (await api.post(followup_path, json={"task_ids": []})).json() == []
    async with api.fixture_sessions() as session:  # type: ignore[attr-defined]
        checkpoint = await session.scalar(
            select(WorkflowCheckpointRow.payload).where(
                WorkflowCheckpointRow.task_id == followup_id
            )
        )
        assert checkpoint is not None
        snapshot = Snapshot.model_validate(checkpoint)
        assert snapshot.limits.total_tokens == 500
        history = snapshot.restore({}, 0).task_reference_payload()["prior_task_results_untrusted"]
        assert [item["kind"] for item in history] == ["continuation", "reference"]
        assert history[0]["answer_excerpt"].startswith("Original text.")
        assert history[0]["answer_excerpt"].endswith("Final paragraph.")
        assert len(history[0]["answer_excerpt"]) <= 4000
        assert history[1]["answer_excerpt"] == "Selected reference result."
        row = await session.get(AgentTaskRow, original)
        assert row is not None and row.answer == long_answer
        clean = await session.scalar(
            select(WorkflowCheckpointRow.payload).where(WorkflowCheckpointRow.task_id == active)
        )
        assert not Snapshot.model_validate(clean).restore({}, 0).task_reference_payload()
        # Set the fixture job's terminal state before exercising normal deletion rules.
        job = await session.get(WorkflowJobRow, original)
        assert job is not None
        job.status = "succeeded"
        await session.commit()

    removed = await api.delete(f"{project_path}/tasks/{original}")
    assert removed.status_code == 204, removed.text
    assert (await api.get(followup_path)).json()["previous"] is None
    project_tasks = (await api.get(f"{project_path}/tasks")).json()
    assert {item["task_id"] for item in project_tasks["items"]} == {followup_id, reference, active}
    replay = await submit(
        "Expand the comparison",
        continuation_of_task_id=original,
        reference_task_ids=[reference],
        budget={"total_tokens": 500},
    )
    assert replay.status_code == 202 and replay.json()["task_id"] == followup_id
    assert not replay.json()["created"]
