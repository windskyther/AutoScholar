from pathlib import Path

import httpx
import pytest

from autoscholar.llm.profiles import ModelProfileCreate
from autoscholar.orchestration.checkpoints import Snapshot
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.models import TaskPlan
from autoscholar.orchestration.service import WorkflowError
from autoscholar.orchestration.task_types import TASK_TYPES, task_guidance
from autoscholar.rag.database_models import DocumentRow, ProjectRow
from tests import test_model_profiles
from tests.test_agent_runner import ScriptedProvider
from tests.test_durable_workflow import drain
from tests.test_model_profiles import AUTH, connection
from tests.test_research_workflow import OBJECTIVE, RESEARCH_PLAN, research_script, research_service

client = test_model_profiles.client


@pytest.mark.parametrize(
    "kind", ["research", "literature_review", "comparison", "research_proposal"]
)
async def test_task_type_survives_restarts_and_reaches_real_research_pipeline(
    tmp_path: Path, kind: str
) -> None:
    provider = ScriptedProvider(research_script())
    service, engine = await research_service(tmp_path, provider)
    try:
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            {"objective": OBJECTIVE, "workflow_kind": kind, "project_id": "research-project"},
            "typed-task",
        )
        assert await drain(DurableService(service), task_id) == "succeeded"
        snapshot, _ = await durable.repository.snapshot(task_id)
        assert snapshot.workflow_kind == kind and OBJECTIVE in (snapshot.answer or "")
        assert task_guidance(kind) in provider.calls[0]["messages"][0].content
        assert any(
            task_guidance(kind) in message.content
            for call in provider.calls
            for message in call["messages"]
            if isinstance(getattr(message, "content", None), str)
        )
        assert not await service.tasks.list_experiments(task_id)
    finally:
        await engine.dispose()


async def test_document_analysis_is_restricted_to_project_sources(tmp_path: Path) -> None:
    service, engine = await research_service(tmp_path, ScriptedProvider([]))
    try:
        run = Snapshot(
            task_id="docs",
            objective=OBJECTIVE,
            workflow_kind="document_analysis",
            project_id="research-project",
            document_ids=["doc"],
        ).restore({}, 0)
        plan = TaskPlan.model_validate(RESEARCH_PLAN)
        with pytest.raises(WorkflowError, match="only use selected project documents"):
            service._validate_plan(run, plan)
        knowledge = plan.model_copy(deep=True)
        knowledge.steps[0].type = "knowledge"
        service._validate_plan(run, knowledge)
    finally:
        await engine.dispose()


async def test_all_http_model_entrypoints_require_managed_selection(
    client: httpx.AsyncClient,
) -> None:
    for path, body in [
        ("/chat", {"message": "test"}),
        ("/agent/run", {"objective": "test"}),
        ("/workbench/agent/tasks", {"objective": "test", "mode": "autonomous"}),
        ("/projects/any/rag/query", {"question": "test"}),
    ]:
        response = await client.post(
            path, json=body, headers=AUTH | {"Idempotency-Key": "no-model"}
        )
        assert response.status_code == 422, response.text
    catalog = (await client.get("/workbench/models", headers=AUTH)).json()
    assert "default_model" not in catalog
    capabilities = (await client.get("/workbench/session", headers=AUTH)).json()["capabilities"]
    assert {item["id"] for item in capabilities["task_types"]} == set(TASK_TYPES)
    assert capabilities["research_quality"] is True


@pytest.mark.parametrize("depth", [None, "quick", "standard", "deep"])
async def test_public_research_depth_is_frozen_and_idempotent(
    client: httpx.AsyncClient,
    depth: str | None,
) -> None:
    profile = connection()
    assert (await client.post("/workbench/models", json=profile)).status_code == 201
    payload = dict(
        objective="Public research fixture",
        mode="autonomous",
        workflow_kind="research",
        model_profile_id=profile["id"],
        document_ids=[],
    )
    if depth is not None:
        payload["research_depth"] = depth
    first = await client.post(
        "/workbench/agent/tasks", json=payload, headers={"Idempotency-Key": "quality-default"}
    )
    assert first.status_code == 202, first.text
    repeated = await client.post(
        "/workbench/agent/tasks", json=payload, headers={"Idempotency-Key": "quality-default"}
    )
    assert not repeated.json()["created"]
    app = client.fixture_app  # type: ignore[attr-defined]
    snapshot, _ = await app.state.durable_service.repository.snapshot(first.json()["task_id"])
    assert snapshot.research_depth == (depth or "standard")
    audit = await client.get(f"/workbench/tasks/{first.json()['task_id']}/quality")
    assert audit.status_code == 200 and audit.json()["depth"] == (depth or "standard")
    assert (await client.get("/workbench/tasks/does-not-exist/quality")).status_code == 404


async def test_new_legacy_tasks_and_empty_document_analysis_are_rejected_without_calls(
    client: httpx.AsyncClient,
) -> None:
    profile = connection()
    assert (await client.post("/workbench/models", json=profile, headers=AUTH)).status_code == 201
    for kind in ["experiment", "document_analysis"]:
        response = await client.post(
            "/workbench/agent/tasks",
            json={
                "objective": "test",
                "mode": "autonomous",
                "workflow_kind": kind,
                "model_profile_id": profile["id"],
                "document_ids": [],
            },
            headers=AUTH | {"Idempotency-Key": kind},
        )
        assert response.status_code == 422
    for prefix in ["", "/workbench"]:
        assert (
            await client.get(prefix + "/agent/tasks/any/experiments", headers=AUTH)
        ).status_code in {404, 405}
    app = client.fixture_app  # type: ignore[attr-defined]
    assert not any(
        getattr(route, "path", "").endswith("/{task_id}/experiments") for route in app.routes
    )


async def test_synchronous_chat_uses_the_selected_registered_model(
    client: httpx.AsyncClient,
) -> None:
    from tests.test_chat import SuccessfulProvider

    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    profile = await profiles.create(ModelProfileCreate.model_validate(connection()))
    profiles.providers[profile.id] = SuccessfulProvider()
    response = await client.post(
        "/chat", json={"message": "test", "model_profile_id": profile.id}, headers=AUTH
    )
    assert response.status_code == 200 and response.json()["answer"] == "The answer"


async def test_document_analysis_freezes_only_ready_project_documents(
    client: httpx.AsyncClient,
) -> None:
    profile = connection()
    assert (await client.post("/workbench/models", json=profile)).status_code == 201
    project_id = "local-documents-project"
    app = client.fixture_app  # type: ignore[attr-defined]
    async with app.state.model_profiles.sessions() as session:
        session.add(ProjectRow(id=project_id, name="Local documents"))
        await session.flush()
        for identifier, status in [("ready-doc", "ready"), ("pending-doc", "queued")]:
            session.add(
                DocumentRow(
                    id=identifier,
                    project_id=project_id,
                    title=identifier,
                    original_filename="fixture.pdf",
                    content_type="application/pdf",
                    storage_key=identifier,
                    sha256=identifier.ljust(64, "0"),
                    size_bytes=100,
                    status=status,
                )
            )
        await session.commit()
    response = await client.post(
        "/workbench/agent/tasks",
        json={
            "objective": "Analyze documents",
            "mode": "autonomous",
            "workflow_kind": "document_analysis",
            "project_id": project_id,
            "model_profile_id": profile["id"],
            "document_ids": None,
        },
        headers={"Idempotency-Key": "ready-doc-analysis"},
    )
    assert response.status_code == 202, response.text
    snapshot, _ = await app.state.durable_service.repository.snapshot(response.json()["task_id"])
    assert snapshot.document_ids == ["ready-doc"]
    assert snapshot.workflow_kind == "document_analysis"
