"""Local goal removal and full Markdown export never dispatch research operations."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from autoscholar.core.budget import current_parent
from autoscholar.core.config import Settings
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowEventRow, WorkflowJobRow
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.tool_platform.operation_models import CoreToolCallRow
from tests.governance import offline_app
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

AUTH = {"Authorization": "Bearer public-task-actions-token"}


@pytest.fixture
async def actions(tmp_path: Path) -> AsyncIterator[tuple[Any, ...]]:
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    knowledge = KnowledgeRepository(service.tasks.session_factory)
    project = await knowledge.create_project(name="目标管理", description=None)
    other = await knowledge.create_project(name="其他项目", description=None)
    for task_id in ("goal", "keep"):
        await service.tasks.create_task(
            task_id=task_id, objective="中文目标 / 输出", mode="research", project_id=project.id
        )
        await service.tasks.update_task(
            task_id,
            status="succeeded",
            plan=[],
            answer="中文结论 [E1].\n",
            metrics={"total_tokens": 12},
            mode="research",
        )
    token = current_parent.set("goal")
    try:
        await service.tasks.create_task(
            task_id="child", objective="子任务", mode="research", project_id=project.id
        )
        await service.tasks.update_task(
            "child", status="succeeded", plan=[], answer="子结果", metrics={}, mode="research"
        )
    finally:
        current_parent.reset(token)
    app = offline_app(
        Settings(model_encryption_key=SecretStr("public-task-actions-token")),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=knowledge,
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, service, knowledge, project.id, other.id
        assert not provider.calls
    finally:
        await engine.dispose()


async def test_remove_one_goal_hides_all_workbench_views_and_keeps_other_goals_and_receipts(
    actions: tuple[Any, ...],
) -> None:
    client, service, knowledge, project_id, _ = actions
    path = f"/workbench/projects/{project_id}/tasks/goal"
    assert (await client.delete(path, headers=AUTH)).status_code == 204
    assert (await client.delete(path, headers=AUTH)).status_code == 204
    listed = await client.get(f"/workbench/projects/{project_id}/tasks", headers=AUTH)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert [item["task_id"] for item in listed.json()["items"]] == ["keep"]
    for suffix in (
        "overview",
        "controls",
        "approvals",
        "events",
        "resources/evidence",
        "output/markdown",
    ):
        assert (
            await client.get(f"/workbench/tasks/goal/{suffix}", headers=AUTH)
        ).status_code == 404
    original = await service.tasks.get_task("goal")
    assert original and original.answer == "中文结论 [E1].\n" and original.status == "succeeded"
    assert original.metrics == {"total_tokens": 12}
    assert await service.tasks.get_task("child") is not None
    assert await knowledge.get_project(project_id) is not None
    async with service.tasks.session_factory() as session:
        events = (
            await session.scalars(
                select(WorkflowEventRow).where(WorkflowEventRow.task_id == "goal")
            )
        ).all()
        assert [(event.sequence, event.kind) for event in events] == [(1, "task_deleted")]


@pytest.mark.parametrize("guard", ["running", "child", "pending", "receipt", "paused"])
async def test_goal_removal_refuses_active_or_uncertain_family_operations(
    actions: tuple[Any, ...], guard: str
) -> None:
    client, service, _, project_id, _ = actions
    if guard in {"running", "child"}:
        task_id = "child" if guard == "child" else "goal"
        await service.tasks.update_task(
            task_id, status="running", plan=[], answer=None, metrics={}, mode="research"
        )
    else:
        async with service.tasks.session_factory() as session:
            if guard != "receipt":
                session.add(
                    WorkflowJobRow(
                        task_id="goal",
                        idempotency_key="goal-key",
                        request_sha256="a" * 64,
                        status="paused" if guard == "paused" else "recovery_required",
                        pending_calls={"call": "llm"} if guard == "pending" else {},
                    )
                )
            if guard == "receipt":
                session.add(
                    CoreToolCallRow(
                        id="call",
                        task_id="child",
                        service="fixture",
                        tool="search",
                        arguments_sha256="a" * 64,
                        authority_sha256="b" * 64,
                        output_schema={},
                        status="unknown",
                    )
                )
            await session.commit()
    result = await client.delete(f"/workbench/projects/{project_id}/tasks/goal", headers=AUTH)
    assert (
        result.status_code == 409 and result.json()["error"]["code"] == "task_has_active_operations"
    )
    assert (await client.get("/workbench/tasks/goal/overview", headers=AUTH)).status_code == 200


async def test_cancel_then_remove_preserves_uncertain_receipts_and_stale_children(
    actions: tuple[Any, ...],
) -> None:
    client, service, _, project_id, _ = actions
    await service.tasks.update_task(
        "child",
        status="running",
        plan=[],
        answer=None,
        metrics={"total_tokens": 9},
        mode="research",
    )
    async with service.tasks.session_factory() as session:
        session.add(
            WorkflowJobRow(
                task_id="goal",
                idempotency_key="cancel-then-delete",
                request_sha256="a" * 64,
                status="recovery_required",
                pending_calls={"call": "llm"},
                active={"stage": "executor", "step_id": "research", "version": 1},
                usage={"model_calls": 1, "total_tokens": 9},
            )
        )
        session.add(
            CoreToolCallRow(
                id="call",
                task_id="child",
                service="fixture",
                tool="search",
                arguments_sha256="a" * 64,
                authority_sha256="b" * 64,
                output_schema={},
                status="unknown",
            )
        )
        await session.commit()
    durable = DurableService(service)
    assert await durable.repository.control("goal", "cancel") == "cancelled"
    path = f"/workbench/projects/{project_id}/tasks/goal"
    assert (await client.delete(path, headers=AUTH)).status_code == 204
    assert (await client.delete(path, headers=AUTH)).status_code == 204
    assert (await client.get("/workbench/tasks/goal/overview", headers=AUTH)).status_code == 404
    async with service.tasks.session_factory() as session:
        job = await session.get(WorkflowJobRow, "goal")
        receipt = await session.get(CoreToolCallRow, "call")
        assert job and job.status == "cancelled" and job.pending_calls == {"call": "llm"}
        assert job.active == {"stage": "executor", "step_id": "research", "version": 1}
        assert job.usage == {"model_calls": 1, "total_tokens": 9}
        assert job.owner is None and job.lease_until is None
        assert receipt and receipt.status == "unknown" and receipt.result is None
        events = (
            await session.scalars(
                select(WorkflowEventRow)
                .where(WorkflowEventRow.task_id == "goal")
                .order_by(WorkflowEventRow.sequence)
            )
        ).all()
        assert [event.kind for event in events] == ["cancel", "task_deleted"]
    child = await service.tasks.get_task("child")
    assert child and child.status == "running" and child.metrics == {"total_tokens": 9}
    assert await service.tasks.get_task("keep") is not None
    assert not await durable.tick("goal")  # removing the row from the UI never requeues work


@pytest.mark.parametrize("guard", ["owner", "lease", "child_job"])
async def test_cancelled_goal_removal_still_refuses_live_owners_and_independent_jobs(
    actions: tuple[Any, ...],
    guard: str,
) -> None:
    client, service, _, project_id, _ = actions
    async with service.tasks.session_factory() as session:
        session.add(
            WorkflowJobRow(
                task_id="goal",
                idempotency_key="stopped-root",
                request_sha256="a" * 64,
                status="cancelled",
                owner="worker" if guard == "owner" else None,
                lease_until=datetime.now(UTC) + timedelta(minutes=1) if guard == "lease" else None,
            )
        )
        if guard == "child_job":
            session.add(
                WorkflowJobRow(
                    task_id="child",
                    idempotency_key="live-child",
                    request_sha256="b" * 64,
                    status="running",
                    owner="child-worker",
                    lease_until=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
        await session.commit()
    result = await client.delete(f"/workbench/projects/{project_id}/tasks/goal", headers=AUTH)
    assert result.status_code == 409
    assert (await client.get("/workbench/tasks/goal/overview", headers=AUTH)).status_code == 200


async def test_goal_removal_requires_auth_and_correct_project_root(
    actions: tuple[Any, ...],
) -> None:
    client, _, _, project_id, other_id = actions
    path = f"/workbench/projects/{project_id}/tasks/goal"
    assert (
        await client.delete(path, headers={"Origin": "https://foreign.invalid"})
    ).status_code == 403
    assert (
        await client.delete(f"/workbench/projects/{other_id}/tasks/goal", headers=AUTH)
    ).status_code == 404
    assert (
        await client.delete(f"/workbench/projects/{project_id}/tasks/child", headers=AUTH)
    ).status_code == 404
    assert (
        await client.delete(f"/workbench/projects/{project_id}/tasks/missing", headers=AUTH)
    ).status_code == 404


async def test_markdown_export_preserves_full_utf8_output_beyond_the_preview(
    actions: tuple[Any, ...],
) -> None:
    client, service, _, _, _ = actions
    answer = "## 数学与引用\n\\(y=x^2\\) [E1]\n" + "完整中文证据与结论。\n" * 40000
    await service.tasks.update_task(
        "goal", status="succeeded", plan=[], answer=answer, metrics={}, mode="research"
    )
    preview = await client.get("/workbench/tasks/goal/overview", headers=AUTH)
    assert preview.json()["answer_truncated"]
    response = await client.get("/workbench/tasks/goal/output/markdown", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "attachment;" in response.headers["content-disposition"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.content.decode("utf-8") == "# 中文目标 / 输出\n\n" + answer


async def test_markdown_export_requires_auth_visible_root_and_existing_output(
    actions: tuple[Any, ...],
) -> None:
    client, service, _, _, _ = actions
    assert (
        await client.get(
            "/workbench/tasks/goal/output/markdown", headers={"Origin": "https://foreign.invalid"}
        )
    ).status_code == 403
    assert (
        await client.get("/workbench/tasks/child/output/markdown", headers=AUTH)
    ).status_code == 404
    await service.tasks.update_task(
        "goal", status="failed", plan=[], answer=None, metrics={}, mode="research"
    )
    result = await client.get("/workbench/tasks/goal/output/markdown", headers=AUTH)
    assert result.status_code == 409 and result.json()["error"]["code"] == "task_output_unavailable"
    caps = (await client.get("/workbench/session", headers=AUTH)).json()["capabilities"]
    assert caps["task_deletion"] and caps["markdown_export"]
