"""Deterministic fault tests: no external model or search requests."""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.coding.sandbox import SandboxRunRequest, SandboxRunResult
from autoscholar.core.budget import (
    Budget,
    BudgetedLLM,
    BudgetExceeded,
    BudgetLimits,
    current_budget,
    current_parent,
)
from autoscholar.core.config import Settings
from autoscholar.llm import LLMResult, ToolCall
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.main import create_app
from autoscholar.orchestration.smoke import run_smoke
from autoscholar.research import SearchProviderError, TavilySearchProvider
from tests.test_agent_runner import ScriptedProvider, response
from tests.test_autonomous import InvalidOnceSandbox, call, script, workflow
from tests.test_experiment_service import FakeExperimentSandbox, _artifact
from tests.test_health import FakeDependency
from tests.test_llm_provider import configured_settings
from tests.test_workflow_foundation import plan


async def test_persistent_fault_mode_refuses_paid_provider() -> None:
    with pytest.raises(ValueError, match="requires --offline"):
        await run_smoke(inject_fault=False, research=False, persistent_fault=True)


@pytest.mark.parametrize(
    "failure,code",
    [
        ("timeout", "llm_timeout"),
        ("401", "llm_authentication_failed"),
        ("429", "llm_rate_limited"),
        ("503", "llm_upstream_error"),
    ],
)
async def test_workflow_upstream_failure_does_not_retry_or_leak_secrets(
    tmp_path: Path,
    failure: str,
    code: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private-upstream-detail", request=request)
        return httpx.Response(
            int(failure),
            json={
                "error": {
                    "message": "private-upstream-detail",
                    "type": "upstream_error",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(configured_settings(), http_client=client)
        sandbox = FakeExperimentSandbox()
        service, engine = await workflow(tmp_path, provider, sandbox)
        try:
            result = await service.run("Compare")
            assert result.task.status == "failed"
            assert result.task.error_code == code
            assert "private-upstream-detail" not in (result.task.error_message or "")
            assert result.task.metrics["model_calls"] == calls == 1
            assert not sandbox.requests
            assert not await service.workflows.history(result.task.id, "steps")
        finally:
            await engine.dispose()


@pytest.mark.parametrize(
    "failure,expected_calls",
    [
        ("timeout", 3),
        ("429", 3),
        ("503", 3),
        ("401", 1),
        ("invalid_json", 1),
        ("empty", 1),
    ],
)
async def test_tavily_failures_have_bounded_retries_without_secret_leak(
    failure: str,
    expected_calls: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private-test-key", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"private-test-key")
        if failure == "empty":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(int(failure), text="private-test-key")

    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TavilySearchProvider(
            api_key="private-test-key",
            client=client,
            retry_delays=(0, 0),
            sleep=no_sleep,
        )
        if failure == "empty":
            assert await provider.search("MNIST", limit=3) == []
        else:
            with pytest.raises(SearchProviderError) as rejected:
                await provider.search("MNIST", limit=3)
            assert "private-test-key" not in str(rejected.value)
        assert calls == expected_calls


async def test_all_autonomous_routes_reject_wrong_tokens_and_cross_task_artifacts(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([*script(), *script()])
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    app = create_app(
        Settings(experiment_api_token=SecretStr("resilience-token")),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        workspace_manager=service.workspace,
        sandbox_executor=FakeExperimentSandbox(),
    )
    app.state.autonomous_service = service
    auth = {"Authorization": "Bearer resilience-token"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            results = []
            for _ in range(2):
                reply = await client.post(
                    "/agent/run", headers=auth, json={"objective": "Compare", "mode": "autonomous"}
                )
                assert reply.status_code == 200
                results.append(reply.json()["task_id"])
            steps = await service.workflows.history(results[0], "steps")
            code = next(s["child_task_id"] for s in steps if s["step_id"] == "code")
            train = next(s["child_task_id"] for s in steps if s["step_id"] == "train")
            artifact = (await service.tasks.list_artifacts(train))[0]
            experiment = (await service.tasks.list_experiments(train))[0]
            paths = [
                f"/agent/tasks/{results[0]}",
                *[
                    f"/agent/tasks/{results[0]}/workflow/{kind}"
                    for kind in ("plans", "steps", "reviews")
                ],
                f"/agent/tasks/{code}",
                f"/agent/tasks/{code}/evidence",
                f"/agent/tasks/{code}/workspace",
                f"/agent/tasks/{code}/workspace/files/source/train.py",
                f"/agent/tasks/{train}/experiments",
                f"/agent/tasks/{train}/experiments/{experiment.id}",
                f"/agent/tasks/{train}/artifacts",
                f"/agent/tasks/{train}/artifacts/{artifact.id}",
            ]
            for path in paths:
                for authorization in (
                    b"",
                    b"Bearer wrong",
                    b"Basic resilience-token",
                    b"Bearer \xff",
                ):
                    denied = await client.get(path, headers=[(b"authorization", authorization)])
                    assert denied.status_code == 401, (path, authorization, denied.text)
                    assert "resilience-token" not in denied.text
                assert (await client.get(path, headers=auth)).status_code == 200
            other_steps = await service.workflows.history(results[1], "steps")
            for authorization in (b"Bearer wrong", b"Bearer \xff"):
                denied = await client.post(
                    "/agent/run",
                    headers=[(b"authorization", authorization)],
                    json={"objective": "Must not execute", "mode": "autonomous"},
                )
                assert denied.status_code == 401
            assert len(provider.calls) == 6
            other = next(s["child_task_id"] for s in other_steps if s["step_id"] == "train")
            for path in (
                f"/agent/tasks/{other}/artifacts/{artifact.id}",
                f"/agent/tasks/{other}/experiments/{experiment.id}",
            ):
                assert (await client.get(path, headers=auth)).status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "resource",
    [
        "steps",
        "replans",
        "model_calls",
        "tool_calls",
        "search_queries",
        "code_repairs",
        "training_runs",
        "sandbox_runs",
    ],
)
def test_each_counter_stops_at_limit_without_reset(resource: str) -> None:
    budget = Budget(BudgetLimits.model_validate({resource: 2}))
    budget.consume(resource)
    budget.consume(resource)
    with pytest.raises(BudgetExceeded, match=resource):
        budget.consume(resource)
    assert budget.used[resource] == 2


@pytest.mark.parametrize("limit,expected_calls", [(2, 1), (3, 1), (4, 2)])
async def test_token_threshold_stops_next_call(limit: int, expected_calls: int) -> None:
    provider = ScriptedProvider([response() for _ in range(3)])
    metered = BudgetedLLM(provider)
    budget = Budget(BudgetLimits(total_tokens=limit))
    token = current_budget.set(budget)
    try:
        with pytest.raises(BudgetExceeded, match="total_tokens"):
            for _ in range(3):
                await metered.generate([])
        with pytest.raises(BudgetExceeded):
            await metered.generate([])
        assert len(provider.calls) == expected_calls
        assert budget.used["total_tokens"] == 3 * expected_calls
    finally:
        current_budget.reset(token)


@pytest.mark.parametrize("resource", ["steps", "tool_calls", "sandbox_runs"])
async def test_workflow_budget_stops_physical_dispatch(tmp_path: Path, resource: str) -> None:
    provider = ScriptedProvider(script())
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare", budget=BudgetLimits.model_validate({resource: 1}))
        assert result.task.status == "budget_exceeded"
        assert result.task.answer is None
        assert not any(r.action == "run_python" for r in sandbox.requests)
        assert len(sandbox.requests) == (2 if resource == "steps" else 1)
        assert len(provider.calls) == 2
        assert all(
            row["status"] != "running"
            for row in await service.workflows.history(result.task.id, "steps")
        )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("case", ["goal", "type", "specification"])
async def test_revision_cannot_change_contract(tmp_path: Path, case: str) -> None:
    revised = plan().model_dump()
    if case == "goal":
        revised["goal"] = "Changed objective"
    elif case == "type":
        revised["steps"][1]["type"] = "coding"
        revised["steps"].append(
            {
                "id": "extra",
                "type": "experiment",
                "description": "Train",
                "expected_output": "Metrics",
                "dependencies": ["code"],
            }
        )
    else:
        revised["steps"][1]["specification"] = {"seed": 43}
    replies = script(replan=True)
    replies[3] = call(
        "submit_plan_revision",
        {
            "plan": revised,
            "rerun_steps": ["train"],
            "reason": "Invalid revision",
        },
    )
    provider = ScriptedProvider(replies)
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare")
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert len(await service.workflows.history(result.task.id, "plans")) == 1
        assert result.task.metrics["training_runs"] == 1
        assert len(provider.calls) == 4
    finally:
        await engine.dispose()


@pytest.mark.parametrize("case", ["empty", "wrong_name", "multiple"])
async def test_protocol_failures_stop_after_two_attempts(tmp_path: Path, case: str) -> None:
    bad = response()
    if case == "wrong_name":
        bad = call("invented_tool", {})
    elif case == "multiple":
        bad = LLMResult(
            text="",
            model="test",
            usage=response().usage,
            tool_calls=tuple(
                ToolCall(id=str(i), name="submit_task_plan", arguments=plan().model_dump())
                for i in range(2)
            ),
        )
    provider = ScriptedProvider([bad, bad])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare")
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert len(provider.calls) == 2
        assert result.task.metrics["total_tokens"] == 6
        assert not sandbox.requests
    finally:
        await engine.dispose()


class CorruptSandbox(FakeExperimentSandbox):
    def __init__(self, case: str) -> None:
        super().__init__()
        self.case = case

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        result = await super().run(request)
        if request.action != "run_python":
            return result
        artifacts = list(result.artifacts)
        if self.case == "missing":
            artifacts.pop(0)
        elif self.case == "hash":
            artifacts[0] = artifacts[0].model_copy(update={"sha256": "0" * 64})
        elif self.case == "json":
            artifacts[0] = _artifact(artifacts[0].path, b"{invalid")
        elif self.case in {"extra_field", "nan"}:
            raw = json.loads(base64.b64decode(artifacts[0].data_base64))
            if self.case == "extra_field":
                raw["primary_metric"] = "test_accuracy"
            else:
                raw["runs"][0]["test_accuracy"] = float("nan")
            artifacts[0] = _artifact(artifacts[0].path, json.dumps(raw).encode())
        elif self.case == "png_truncated":
            artifacts[1] = _artifact(
                artifacts[1].path, base64.b64decode(artifacts[1].data_base64)[:33]
            )
        elif self.case == "checkpoint_truncated":
            artifacts[3] = _artifact(artifacts[3].path, b"PK\x03\x04truncated")
        return result.model_copy(update={"artifacts": artifacts})


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "hash",
        "json",
        "extra_field",
        "nan",
        "png_truncated",
        "checkpoint_truncated",
    ],
)
async def test_corrupt_outputs_never_pass_optimistic_reviewer(tmp_path: Path, case: str) -> None:
    service, engine = await workflow(tmp_path, ScriptedProvider(script()), CorruptSandbox(case))
    try:
        result = await service.run("Compare", budget=BudgetLimits(replans=0))
        assert result.task.status == "budget_exceeded", case
        assert result.task.answer is None
        reviews = await service.workflows.history(result.task.id, "reviews")
        assert reviews[0]["payload"]["status"] == "REPLAN"
        steps = await service.workflows.history(result.task.id, "steps")
        experiment = next(row for row in steps if row["step_id"] == "train")
        assert experiment["status"] == "failed"
        records = await service.tasks.list_experiments(experiment["child_task_id"])
        assert records[0].status == "failed"
    finally:
        await engine.dispose()


class ReviewHookProvider(ScriptedProvider):
    hook: Callable[[dict[str, Any]], Awaitable[None]]

    async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
        if any(tool.name == "submit_review" for tool in kwargs.get("tools", [])):
            await self.hook(json.loads(messages[-1].content))
        return await super().generate(messages, **kwargs)


@pytest.mark.parametrize("case", ["source", "artifact"])
async def test_review_rechecks_integrity_after_model_wait(tmp_path: Path, case: str) -> None:
    provider = ReviewHookProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())

    async def tamper(payload: dict[str, Any]) -> None:
        assert payload["deterministic_issues"] == []
        child = payload["results"]["train"]["child_task_id"]
        if case == "source":
            service.workspace.write_text(child, "train.py", "# changed", overwrite=True)
        else:
            service.workspace.write_bytes(
                child, "metrics.json", b"{}", area="outputs", overwrite=True
            )

    provider.hook = tamper
    try:
        result = await service.run("Compare", budget=BudgetLimits(replans=0))
        assert result.task.status == "budget_exceeded"
        assert result.task.answer is None
        reviews = await service.workflows.history(result.task.id, "reviews")
        assert reviews[0]["payload"]["status"] == "REPLAN"
        assert reviews[0]["payload"]["issues"][0]["code"] == "artifact_validation_failed"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("stage", ["planner", "coding", "experiment", "reviewer"])
async def test_explicit_cancellation_finalizes_all_started_records(
    tmp_path: Path,
    stage: str,
) -> None:
    entered = asyncio.Event()
    blocked_names = {
        "planner": "submit_task_plan",
        "coding": "submit_code_ready",
        "reviewer": "submit_review",
    }

    class BlockingProvider(ScriptedProvider):
        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            if any(t.name == blocked_names.get(stage) for t in kwargs.get("tools", [])):
                entered.set()
                await asyncio.Event().wait()
            return await super().generate(messages, **kwargs)

    class BlockingSandbox(FakeExperimentSandbox):
        async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
            if stage == "experiment" and request.action == "run_python":
                entered.set()
                await asyncio.Event().wait()
            return await super().run(request)

    service, engine = await workflow(tmp_path, BlockingProvider(script()), BlockingSandbox())
    running = asyncio.create_task(service.run("Cancellation test"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        async with service.tasks.session_factory() as session:
            rows = list((await session.scalars(select(AgentTaskRow))).all())
        parent = next(row for row in rows if row.parent_task_id is None)
        assert parent.status == "failed"
        assert parent.error_code == "autonomous_cancelled"
        assert all(row.status != "running" for row in rows)
        for row in rows:
            assert all(e.status != "running" for e in await service.tasks.list_experiments(row.id))
        assert all(
            s["status"] != "running" for s in await service.workflows.history(parent.id, "steps")
        )
        assert current_budget.get() is None and current_parent.get() is None
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await engine.dispose()


async def test_concurrent_workflows_keep_budgets_and_lineage_isolated(tmp_path: Path) -> None:
    barrier = asyncio.Barrier(2)

    class ConcurrentProvider(ScriptedProvider):
        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            names = {t.name for t in kwargs.get("tools", [])}
            self.calls.append({"parent": current_parent.get(), "budget": current_budget.get()})
            if "submit_task_plan" in names:
                await asyncio.wait_for(barrier.wait(), timeout=10)
                return call("submit_task_plan", plan().model_dump())
            if "submit_review" in names:
                return call("submit_review", {"status": "PASS"})
            return call("submit_code_ready", {"summary": "Validate"})

    provider = ConcurrentProvider([])
    service, engine = await workflow(
        tmp_path,
        provider,
        FakeExperimentSandbox(),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'concurrency.db').as_posix()}",
    )
    try:
        stopped, succeeded = await asyncio.gather(
            service.run("Limited task", budget=BudgetLimits(model_calls=1)),
            service.run("Normal task"),
        )
        assert stopped.task.status == "budget_exceeded", stopped.task.error_message
        assert succeeded.task.status == "succeeded", succeeded.task.error_message
        assert stopped.task.metrics["model_calls"] == 1
        assert succeeded.task.metrics["model_calls"] == 3
        seen: set[str] = set()
        for result in (stopped, succeeded):
            for step in await service.workflows.history(result.task.id, "steps"):
                child = await service.tasks.get_task(step["child_task_id"])
                assert child and child.parent_task_id == result.task.id
                assert child.id not in seen
                seen.add(child.id)
                for artifact in await service.tasks.list_artifacts(child.id):
                    assert service.artifacts.read_verified(child.id, artifact)
        assert len({id(item["budget"]) for item in provider.calls}) == 2
        assert current_budget.get() is None and current_parent.get() is None
    finally:
        await engine.dispose()
