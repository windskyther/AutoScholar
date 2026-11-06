"""Offline model management, encrypted persistence and task-scoped provider routing."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.core.redaction import redact_text
from autoscholar.llm.models import ChatMessage
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.llm.profiles import ModelProfileRow, ModelProfiles, current_model_profile
from autoscholar.orchestration.checkpoints import digest
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow
from autoscholar.orchestration.service import Run
from tests.governance import offline_app
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

AUTH = {"Authorization": "Bearer public-model-management-operator"}


def connection(name: str = "Reader") -> dict[str, str]:
    return {
        "id": str(uuid4()),
        "name": name,
        "base_url": "https://models.example/v1",
        "model": "synthetic-" + name,
        "api_key": "public-model-key-" + name,
        "compatibility_profile": "generic",
    }


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(
        tmp_path,
        provider,
        sandbox,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'models.sqlite'}",
    )
    app = offline_app(
        Settings(
            _env_file=None,
            experiment_api_token=SecretStr(  # type: ignore[call-arg]
                "public-model-management-operator",
            ),
        ),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
        research_services=[],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        api.fixture_app = app  # type: ignore[attr-defined]
        try:
            yield api
        finally:
            await app.state.model_profiles.close()
            assert not provider.calls and not sandbox.requests
            await engine.dispose()


async def test_create_list_restart_and_auth_never_disclose_keys(client: httpx.AsyncClient) -> None:
    payload = connection()
    assert (await client.post("/workbench/models", json=payload)).status_code == 401
    created = await client.post("/workbench/models", json=payload, headers=AUTH)
    assert created.status_code == 201
    assert created.json()["name"] == "Reader"
    assert "api_key" not in created.json() and payload["api_key"] not in created.text
    repeated = await client.post("/workbench/models", json=payload, headers=AUTH)
    assert repeated.json() == created.json()
    conflict = await client.post(
        "/workbench/models", json=payload | {"model": "changed"}, headers=AUTH
    )
    assert conflict.status_code == 409
    catalog = await client.get("/workbench/models", headers=AUTH)
    assert len(catalog.json()["items"]) == 1
    assert catalog.json()["default_model"] is None
    assert payload["api_key"] not in catalog.text and "encrypted_key" not in catalog.text
    assert payload["api_key"] not in redact_text("diagnostic " + payload["api_key"])
    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    async with profiles.sessions() as session:
        row = await session.get(ModelProfileRow, payload["id"])
        assert row and payload["api_key"] not in row.encrypted_key
        assert profiles.cipher().decrypt(row.encrypted_key.encode()).decode() == payload["api_key"]
    restarted = ModelProfiles(profiles.sessions, profiles.settings)
    assert (await restarted.list())[0].id == payload["id"]
    changed = ModelProfiles(
        profiles.sessions,
        profiles.settings.model_copy(
            update={
                "experiment_api_token": SecretStr("different-public-operator"),
            }
        ),
    )
    with pytest.raises(AppError, match="Re-enter model credentials"):
        await changed.provider(payload["id"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_url", "https://user:private@models.example/v1"),
        ("base_url", "https://models.example/v1?api_key=private"),
        ("base_url", "http://remote.example/v1"),
        ("base_url", "file:///private"),
        ("base_url", "https://models.example/v1/chat/completions"),
        ("api_key", "line\nbreak"),
        ("name", "   "),
        ("model", "   "),
    ],
)
async def test_invalid_connections_are_rejected_without_echoing_credentials(
    client: httpx.AsyncClient,
    field: str,
    value: str,
) -> None:
    payload = connection() | {field: value}
    response = await client.post("/workbench/models", json=payload, headers=AUTH)
    assert response.status_code == 422
    assert payload["api_key"] not in response.text


async def test_selection_is_frozen_idempotent_and_disable_only_affects_new_tasks(
    client: httpx.AsyncClient,
) -> None:
    payload = connection()
    await client.post("/workbench/models", json=payload, headers=AUTH)
    request = {
        "objective": "Synthetic research",
        "mode": "autonomous",
        "workflow_kind": "research",
        "document_ids": [],
        "model_profile_id": payload["id"],
    }
    headers = AUTH | {"Idempotency-Key": "model-selection"}
    submitted = await client.post("/workbench/agent/tasks", json=request, headers=headers)
    assert submitted.status_code == 202, submitted.text
    task_id = submitted.json()["task_id"]
    app = client.fixture_app  # type: ignore[attr-defined]
    snapshot, _ = await app.state.durable_service.repository.snapshot(task_id)
    assert snapshot.model_profile_id == payload["id"]
    assert (
        snapshot.model_profile_name == payload["name"] and snapshot.model_name == payload["model"]
    )
    assert snapshot.capture(snapshot.restore({}, 0), "writer").model_profile_id == payload["id"]
    overview = await client.get(f"/workbench/tasks/{task_id}/overview", headers=AUTH)
    assert overview.json()["execution"]["model_profile_name"] == payload["name"]
    await client.post(f"/workbench/models/{payload['id']}/disable", headers=AUTH)
    assert (await client.post("/workbench/agent/tasks", json=request, headers=headers)).json()[
        "task_id"
    ] == task_id
    rejected = await client.post(
        "/workbench/agent/tasks", json=request, headers=AUTH | {"Idempotency-Key": "new-task"}
    )
    assert rejected.status_code == 422
    changed = await client.post(
        "/workbench/agent/tasks", json=request | {"model_profile_id": str(uuid4())}, headers=headers
    )
    assert changed.status_code == 409
    direct = await client.post("/workbench/agent/run", json=request, headers=AUTH)
    assert direct.status_code == 422
    async with app.state.model_profiles.sessions() as session:
        checkpoints = await session.scalars(select(WorkflowCheckpointRow))
        assert payload["api_key"] not in json.dumps([row.payload for row in checkpoints])


async def test_durable_workers_route_concurrent_tasks_after_restart_without_key_or_budget_mixup(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = client.fixture_app  # type: ignore[attr-defined]
    wires: list[tuple[str, str]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wires.append((body["model"], request.headers["authorization"]))
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Offline answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            },
        )

    def provider(settings: Settings) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr("autoscholar.llm.profiles.create_llm_provider", provider)
    payloads = [connection("Reader"), connection("Writer")]
    task_ids = []
    for index, payload in enumerate(payloads):
        await client.post("/workbench/models", json=payload, headers=AUTH)
        result = await client.post(
            "/workbench/agent/tasks",
            json={
                "objective": "Synthetic task",
                "mode": "autonomous",
                "workflow_kind": "research",
                "document_ids": [],
                "model_profile_id": payload["id"],
            },
            headers=AUTH | {"Idempotency-Key": f"route-{index}"},
        )
        task_ids.append(result.json()["task_id"])
    # A saved connection remains usable by an already queued job even after disabling it.
    await client.post(f"/workbench/models/{payloads[0]['id']}/disable", headers=AUTH)

    async def unit(run: Run, stage: str) -> str:
        result = await app.state.llm_provider.generate(
            [ChatMessage(role="user", content="Synthetic")]
        )
        run.answer = result.text
        return "done"

    workers = [
        DurableService(app.state.autonomous_service),
        DurableService(app.state.autonomous_service),
    ]
    for worker in workers:
        monkeypatch.setattr(worker, "_unit", unit)
    await asyncio.gather(
        *(worker.tick(task_id) for worker, task_id in zip(workers, task_ids, strict=True))
    )
    assert sorted(wires) == sorted((p["model"], "Bearer " + p["api_key"]) for p in payloads)
    for task_id in task_ids:
        snapshot, job = await workers[0].repository.snapshot(task_id)
        assert job.status == "succeeded" and job.usage["total_tokens"] == 7
        assert snapshot.answer == "Offline answer"
    assert current_model_profile.get() is None
    usage = await client.get(
        "/workbench/usage", params={"group_by": "model", "days": 7}, headers=AUTH
    )
    assert usage.status_code == 200
    assert usage.json()["totals"]["total_tokens"] == 14
    assert usage.json()["totals"]["calls"] == 2
    assert {row["label"] for row in usage.json()["groups"]} == {item["model"] for item in payloads}
    async with app.state.model_profiles.sessions() as session:
        jobs = await session.scalars(select(WorkflowJobRow))
        assert all(not row.pending_calls for row in jobs)


async def test_legacy_checkpoint_and_submission_retry_survive_optional_model_fields(
    client: httpx.AsyncClient,
) -> None:
    payload = {"objective": "Old task", "mode": "autonomous", "workflow_kind": "research"}
    headers = AUTH | {"Idempotency-Key": "legacy-model-selection"}
    first = await client.post("/workbench/agent/tasks", json=payload, headers=headers)
    task_id = first.json()["task_id"]
    durable = client.fixture_app.state.durable_service  # type: ignore[attr-defined]
    async with durable.repository.sessions() as session:
        checkpoint = await session.scalar(
            select(WorkflowCheckpointRow).where(
                WorkflowCheckpointRow.task_id == task_id,
            )
        )
        assert checkpoint
        checkpoint.payload = {
            key: value
            for key, value in checkpoint.payload.items()
            if key not in {"model_profile_id", "model_profile_name", "model_name"}
        }
        checkpoint.sha256 = digest(checkpoint.payload)
        await session.commit()
    snapshot, _ = await durable.repository.snapshot(task_id)
    assert snapshot.model_profile_id is None
    retried = await client.post(
        "/workbench/agent/tasks", json=payload | {"model_profile_id": None}, headers=headers
    )
    assert retried.status_code == 202 and retried.json()["task_id"] == task_id
    assert retried.json()["created"] is False
