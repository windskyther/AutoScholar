"""Guarded browser controls/approvals; SQLite and fake providers, no paid API calls."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.main import create_app
from autoscholar.orchestration.approvals import ApprovalDecision
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowApprovalRow, WorkflowJobRow
from autoscholar.orchestration.sandbox import BudgetedSandbox
from autoscholar.rag.repository import KnowledgeRepository
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_durable_workflow import drain
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

AUTH = {"Authorization": "Bearer public-control-test-token"}


@pytest.fixture
async def context(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, DurableService, str]]:
    provider, sandbox = ScriptedProvider(script()), FakeExperimentSandbox()
    service, engine = await workflow(
        tmp_path / "workspace",
        provider,
        sandbox,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'controls.sqlite'}",
    )
    durable = DurableService(service, approval_threshold=0)
    task_id, _ = await durable.submit({"objective": "Public browser control fixture"}, "root")
    await service.tasks.create_task(task_id="legacy", objective="Legacy", mode="research")
    async with durable.repository.sessions() as session:
        session.add(
            AgentTaskRow(
                id="child",
                objective="Child",
                mode="research",
                status="succeeded",
                parent_task_id=task_id,
            )
        )
        await session.commit()
    app = create_app(
        Settings(_env_file=None, experiment_api_token=SecretStr("public-control-test-token")),  # type: ignore[call-arg]
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=KnowledgeRepository(service.tasks.session_factory),
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
        research_services=[],
    )
    app.state.durable_service = durable
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, durable, task_id
    finally:
        await engine.dispose()


async def expected(client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
    response = await client.get(f"/workbench/tasks/{task_id}/controls", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["expected"]  # type: ignore[no-any-return]


def training_runs(durable: DurableService) -> int:
    sandbox = cast(
        FakeExperimentSandbox, cast(BudgetedSandbox, durable.service.coding._sandbox).sandbox
    )
    return sum(request.action == "run_python" for request in sandbox.requests)


@pytest.mark.parametrize("endpoint", ["controls", "approvals"])
async def test_header_auth_and_root_scope(
    context: tuple[httpx.AsyncClient, DurableService, str],
    endpoint: str,
) -> None:
    client, _, task_id = context
    path = f"/workbench/tasks/{task_id}/{endpoint}"
    assert (await client.get(path)).status_code == 401
    assert (
        await client.get(path, params={"token": "public-control-test-token"})
    ).status_code == 401
    for task in ("child", "missing"):
        assert (
            await client.get(f"/workbench/tasks/{task}/{endpoint}", headers=AUTH)
        ).status_code == 404


async def test_legacy_no_controls_and_auth_on_mutations(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, _, task_id = context
    legacy = (await client.get("/workbench/tasks/legacy/controls", headers=AUTH)).json()
    assert legacy["expected"] is None and legacy["actions"] == []
    for path in (
        f"/workbench/tasks/{task_id}/control/cancel",
        f"/workbench/tasks/{task_id}/approvals/fake/decision",
    ):
        assert (await client.post(path, json={})).status_code == 401
    assert (
        await client.post(
            "/workbench/tasks/child/control/cancel",
            headers=AUTH,
            json={"expected": await expected(client, task_id)},
        )
    ).status_code == 404


async def test_pause_resume_cancel_and_duplicate_fencing(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    base = f"/workbench/tasks/{task_id}"
    state = (await client.get(base + "/controls", headers=AUTH)).json()
    assert state["actions"] == ["pause", "cancel"]
    for action, target in (("pause", "paused"), ("resume", "queued"), ("cancel", "cancelled")):
        body = {"expected": await expected(client, task_id)}
        response = await client.post(base + "/control/" + action, json=body, headers=AUTH)
        assert response.status_code == 200 and response.json()["status"] == target
        duplicate = await client.post(base + "/control/" + action, json=body, headers=AUTH)
        assert (
            duplicate.status_code == 409
            and duplicate.json()["error"]["code"] == "workbench_state_stale"
        )
    assert (await client.get(base + "/controls", headers=AUTH)).json()["actions"] == []
    assert not await durable.tick(task_id)
    events = await durable.repository.history(task_id, "events")
    assert [item["kind"] for item in events] == ["cancel", "resume", "pause", "submitted"]


@pytest.mark.parametrize("field", ["status", "checkpoint_sequence", "event_sequence"])
async def test_stale_expectations_do_not_write(
    context: tuple[httpx.AsyncClient, DurableService, str],
    field: str,
) -> None:
    client, durable, task_id = context
    state = await expected(client, task_id)
    state[field] = "paused" if field == "status" else state[field] + 1
    response = await client.post(
        f"/workbench/tasks/{task_id}/control/cancel", headers=AUTH, json={"expected": state}
    )
    assert response.status_code == 409
    assert (await expected(client, task_id))["status"] == "queued"
    assert len(await durable.repository.history(task_id, "events")) == 1


async def test_event_only_change_fences_confirmation(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    state = await expected(client, task_id)
    async with durable.repository.sessions() as session:
        await durable.repository.event(session, task_id, "memory_enabled_changed", enabled=False)
        await session.commit()
    assert (
        await client.post(
            f"/workbench/tasks/{task_id}/control/cancel", headers=AUTH, json={"expected": state}
        )
    ).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"expected": {"status": "queued", "checkpoint_sequence": 1, "event_sequence": True}},
        {"expected": {"status": "queued", "checkpoint_sequence": 0, "event_sequence": 1}},
        {"expected": {"status": "queued", "checkpoint_sequence": 1, "event_sequence": "1"}},
        {
            "expected": {"status": "queued", "checkpoint_sequence": 1, "event_sequence": 1},
            "force": True,
        },
    ],
)
async def test_control_strict_body(
    context: tuple[httpx.AsyncClient, DurableService, str], body: dict[str, Any]
) -> None:
    client, _, task_id = context
    assert (
        await client.post(f"/workbench/tasks/{task_id}/control/cancel", headers=AUTH, json=body)
    ).status_code == 422


@pytest.mark.parametrize(
    "status,actions",
    [
        ("running", ["pause", "cancel"]),
        ("pause_requested", ["cancel"]),
        ("paused", ["resume", "cancel"]),
        ("awaiting_approval", ["cancel"]),
        ("recovery_required", ["cancel"]),
        ("cancel_requested", []),
        ("succeeded", []),
    ],
)
async def test_state_based_controls(
    context: tuple[httpx.AsyncClient, DurableService, str], status: str, actions: list[str]
) -> None:
    client, durable, task_id = context
    async with durable.repository.sessions() as session:
        job = await session.get(WorkflowJobRow, task_id)
        assert job
        await durable.repository.status(session, job, status)
        await session.commit()
    state = (await client.get(f"/workbench/tasks/{task_id}/controls", headers=AUTH)).json()
    assert state["actions"] == actions
    if "resume" not in actions:
        assert (
            await client.post(
                f"/workbench/tasks/{task_id}/control/resume",
                headers=AUTH,
                json={"expected": state["expected"]},
            )
        ).status_code == 409


async def approval(
    client: httpx.AsyncClient, durable: DurableService, task_id: str
) -> dict[str, Any]:
    assert await drain(durable, task_id) == "awaiting_approval"
    response = await client.get(f"/workbench/tasks/{task_id}/approvals", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["items"][0]  # type: ignore[no-any-return]


async def test_approval_projection_pagination_and_plan_scope(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    item = await approval(client, durable, task_id)
    assert item["actions"] == ["approve", "reject", "modify"]
    async with durable.repository.sessions() as session:
        row = await session.get(WorkflowApprovalRow, item["id"])
        assert row
        row.payload = dict(row.payload) | {"private_context": "NEVER_EXPORTED"}
        await session.commit()
    path = f"/workbench/tasks/{task_id}/approvals"
    response = await client.get(path, headers=AUTH)
    assert "NEVER_EXPORTED" not in response.text and "source_sha256" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert (await client.get(path + "?offset=1", headers=AUTH)).json()["items"] == []
    assert (await client.get(path + "?limit=101", headers=AUTH)).status_code == 422


async def test_approve_does_not_resume_and_only_one_training(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    item = await approval(client, durable, task_id)
    path = f"/workbench/tasks/{task_id}/approvals/{item['id']}/decision"
    body = {
        "action": "approve",
        "operation_sha256": item["operation_sha256"],
        "expected": await expected(client, task_id),
    }
    assert (await client.post(path, headers=AUTH, json=body)).json()["status"] == "paused"
    assert (await client.post(path, headers=AUTH, json=body)).status_code == 409
    assert not await durable.tick(task_id)
    assert training_runs(durable) == 0
    resumed = await client.post(
        f"/workbench/tasks/{task_id}/control/resume",
        headers=AUTH,
        json={"expected": await expected(client, task_id)},
    )
    assert resumed.status_code == 200
    assert await drain(durable, task_id) == "succeeded"
    assert training_runs(durable) == 1


async def test_reject_then_modify_and_old_plan_stale(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    item = await approval(client, durable, task_id)
    path = f"/workbench/tasks/{task_id}/approvals/{item['id']}/decision"
    base = {"operation_sha256": item["operation_sha256"], "reason": "Reduce epochs"}
    assert (
        await client.post(
            path,
            headers=AUTH,
            json=base
            | {
                "action": "reject",
                "expected": await expected(client, task_id),
            },
        )
    ).json()["status"] == "awaiting_approval"
    modification = base | {
        "action": "modify",
        "expected": await expected(client, task_id),
        "specification": item["specification"] | {"epochs": 1},
    }
    assert (await client.post(path, headers=AUTH, json=modification)).json()["status"] == "paused"
    snapshot, job = await durable.repository.snapshot(task_id)
    assert snapshot.version == 2 and snapshot.specification.epochs == 1
    assert not snapshot.results and job.usage["model_calls"] == 2
    assert (await client.post(path, headers=AUTH, json=modification)).status_code == 409
    assert not await durable.tick(task_id)
    rows = (await client.get(f"/workbench/tasks/{task_id}/approvals", headers=AUTH)).json()["items"]
    assert rows[0]["status"] == "superseded" and rows[0]["actions"] == []
    # Legacy idempotent approval semantics remain unchanged.
    with pytest.raises(AppError):
        await durable.approvals.decide(
            task_id,
            item["id"],
            ApprovalDecision(action="approve", operation_sha256=item["operation_sha256"]),
        )


@pytest.mark.parametrize("case", ["expired", "hash", "cross_task", "invalid_specification"])
async def test_unsafe_approval_decisions_blocked(
    context: tuple[httpx.AsyncClient, DurableService, str],
    case: str,
) -> None:
    client, durable, task_id = context
    item = await approval(client, durable, task_id)
    body: dict[str, Any] = {
        "action": "approve",
        "operation_sha256": item["operation_sha256"],
        "expected": await expected(client, task_id),
    }
    if case == "expired":
        async with durable.repository.sessions() as session:
            row = await session.get(WorkflowApprovalRow, item["id"])
            assert row
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
    elif case == "hash":
        body["operation_sha256"] = "0" * 64
    elif case == "cross_task":
        task_id, _ = await durable.submit({"objective": "Other"}, "other")
        body["expected"] = await expected(client, task_id)
    else:
        body |= {"action": "modify", "specification": item["specification"] | {"epochs": 0}}
    response = await client.post(
        f"/workbench/tasks/{task_id}/approvals/{item['id']}/decision", headers=AUTH, json=body
    )
    assert (
        response.status_code
        == {"expired": 409, "hash": 409, "cross_task": 404, "invalid_specification": 422}[case]
    )
    assert training_runs(durable) == 0


async def test_concurrent_confirmations_only_one_change(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    body = {"expected": await expected(client, task_id)}
    responses = await asyncio.gather(
        *[
            client.post(f"/workbench/tasks/{task_id}/control/pause", json=body, headers=AUTH)
            for _ in range(4)
        ]
    )
    assert sorted(response.status_code for response in responses) == [200, 409, 409, 409]
    assert len(await durable.repository.history(task_id, "events")) == 2
    # Non-API read-only views never perform a mutation.
    state = await WorkbenchRepository(durable.repository.sessions).controls(task_id)
    assert state.status == "paused"


async def test_concurrent_approvals_only_one_decision(
    context: tuple[httpx.AsyncClient, DurableService, str],
) -> None:
    client, durable, task_id = context
    item = await approval(client, durable, task_id)
    body = {
        "action": "approve",
        "operation_sha256": item["operation_sha256"],
        "expected": await expected(client, task_id),
    }
    responses = await asyncio.gather(
        *[
            client.post(
                f"/workbench/tasks/{task_id}/approvals/{item['id']}/decision",
                headers=AUTH,
                json=body,
            )
            for _ in range(3)
        ]
    )
    assert sorted(response.status_code for response in responses) == [200, 409, 409]
    events = await durable.repository.history(task_id, "events")
    assert sum(item["kind"] == "approval_decided" for item in events) == 1
    assert training_runs(durable) == 0 and not await durable.tick(task_id)
