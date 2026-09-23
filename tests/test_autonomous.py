import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from autoscholar.agent.runner import AgentRunner
from autoscholar.coding.agent import CodingAgent
from autoscholar.coding.sandbox import SandboxRunRequest, SandboxRunResult
from autoscholar.coding.tools import WorkspaceToolset
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import BudgetedLLM, BudgetLimits
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.service import ExperimentService
from autoscholar.llm import LLMResult, ToolCall
from autoscholar.main import create_app
from autoscholar.orchestration.models import ReviewResult, TaskPlan
from autoscholar.orchestration.repository import WorkflowRepository
from autoscholar.orchestration.sandbox import BudgetedSandbox
from autoscholar.orchestration.service import (
    AutonomousService,
    dependency_context,
    experiment_contract,
)
from tests.test_agent_runner import ScriptedProvider, repository, response
from tests.test_experiment_service import FakeExperimentSandbox, _artifact
from tests.test_workflow_foundation import plan


def call(name: str, arguments: dict[str, Any]) -> LLMResult:
    return response(tool_call=ToolCall(id=name, name=name, arguments=arguments))


def script(*, replan: bool = False) -> list[LLMResult]:
    items = [
        call("submit_task_plan", plan().model_dump()),
        call("submit_code_ready", {"summary": "Validated seeded comparison"}),
        call("submit_review", ReviewResult(status="PASS").model_dump()),
    ]
    if replan:
        items.extend(
            [
                call(
                    "submit_plan_revision",
                    {
                        "plan": plan().model_dump(),
                        "rerun_steps": ["train"],
                        "reason": "Retry invalid artifact; preserve validated coding output",
                    },
                ),
                call("submit_review", ReviewResult(status="PASS").model_dump()),
            ]
        )
    return items


class InvalidOnceSandbox(FakeExperimentSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        result = await super().run(request)
        if request.action == "run_python" and not self.failed:
            self.failed = True
            # Process exits successfully but does not produce valid measured results.
            return result.model_copy(
                update={
                    "artifacts": [
                        _artifact("outputs/raw_metrics.json", b'{"bad":true}'),
                        *result.artifacts[1:],
                    ]
                }
            )
        return result


class NegativeResultSandbox(FakeExperimentSandbox):
    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        result = await super().run(request)
        if request.action == "run_python":
            data = json.loads(base64.b64decode(result.artifacts[0].data_base64))
            data["runs"][0]["test_accuracy"] = 0.1
            data["runs"][1]["test_accuracy"] = 0.05
            return result.model_copy(
                update={
                    "artifacts": [
                        _artifact("outputs/raw_metrics.json", json.dumps(data).encode()),
                        *result.artifacts[1:],
                    ]
                }
            )
        return result


async def workflow(
    tmp_path: Path,
    provider: ScriptedProvider,
    sandbox: FakeExperimentSandbox,
) -> tuple[AutonomousService, AsyncEngine]:
    tasks, engine = await repository()
    workspace = WorkspaceManager(tmp_path)
    artifacts = ArtifactManager(workspace, tasks)
    metered = BudgetedLLM(provider)
    isolated = BudgetedSandbox(sandbox)
    coding = CodingAgent(
        provider=metered,
        repository=tasks,
        workspaces=workspace,
        sandbox=isolated,
    )
    runner = AgentRunner(provider=metered, repository=tasks, tools=[], coding_service=coding)
    service = AutonomousService(
        provider=metered,
        tasks=tasks,
        workflows=WorkflowRepository(tasks.session_factory),
        runner=runner,
        coding=coding,
        workspace=workspace,
        artifacts=artifacts,
        experiments=ExperimentService(
            repository=tasks,
            workspace=workspace,
            sandbox=isolated,
            artifacts=artifacts,
            max_repairs=0,
        ),
    )
    return service, engine


async def test_autonomous_passes_with_negative_results_and_real_source_handoff(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(script())
    sandbox = NegativeResultSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        assert "MLP" in (result.task.answer or "")
        assert "10.00%" in (result.task.answer or "")
        assert result.task.metrics["training_runs"] == 1
        steps = await service.workflows.history(result.task.id, "steps")
        by_id = {item["step_id"]: item for item in steps}
        assert (
            by_id["train"]["result"]["source_sha256"]
            == (by_id["train"]["result"]["expected_source_sha256"])
        )
        child = await service.tasks.get_task(by_id["code"]["child_task_id"])
        assert child and child.parent_task_id == result.task.id
        training = next(item for item in sandbox.requests if item.action == "run_python")
        assert training.files["train.py"] == service.workspace.read_text(child.id, "train.py")
        assert len(await service.workflows.history(result.task.id, "reviews")) == 1
        assert all(item["tool_choice"] == "auto" for item in provider.calls)
    finally:
        await engine.dispose()


async def test_rules_override_llm_pass_and_replan_only_affected_step(tmp_path: Path) -> None:
    provider = ScriptedProvider(script(replan=True))
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        assert result.task.metrics["replans"] == 1
        assert result.task.metrics["training_runs"] == 2
        assert result.task.metrics["experiments_started"] == 2
        assert result.task.metrics["experiments_succeeded"] == 1
        assert result.task.metrics["artifact_count"] == 10
        steps = await service.workflows.history(result.task.id, "steps")
        assert len([item for item in steps if item["step_id"] == "code"]) == 1
        runs = [item for item in steps if item["step_id"] == "train"]
        assert len(runs) == 2 and len({item["child_task_id"] for item in runs}) == 2
        reviews = await service.workflows.history(result.task.id, "reviews")
        first = next(item for item in reviews if item["plan_version"] == 1)
        assert first["payload"]["status"] == "REPLAN"
        assert first["payload"]["issues"][0]["code"] == "experiment_metrics_invalid"
        failed = next(item for item in runs if item["status"] == "failed")
        experiments = await service.tasks.list_experiments(failed["child_task_id"])
        assert experiments[0].status == "failed"
        assert len(await service.workflows.history(result.task.id, "plans")) == 2
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "limits,expected_calls",
    [
        (BudgetLimits(model_calls=1), 1),
        (BudgetLimits(replans=0), 3),
        (BudgetLimits(training_runs=1), 4),
    ],
)
async def test_budget_stops_without_extra_calls(
    tmp_path: Path,
    limits: BudgetLimits,
    expected_calls: int,
) -> None:
    provider = ScriptedProvider(script(replan=True))
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models", budget=limits)
        assert result.task.status == "budget_exceeded", result.task.error_message
        assert len(provider.calls) == expected_calls
        assert result.task.answer is None
        assert result.task.metrics["model_calls"] <= limits.model_calls
        assert all(
            item["status"] != "running"
            for item in await service.workflows.history(result.task.id, "steps")
        )
    finally:
        await engine.dispose()


async def test_autonomous_and_child_routes_require_token(tmp_path: Path) -> None:
    from autoscholar.core.config import Settings
    from tests.test_health import FakeDependency

    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    app = create_app(
        Settings(experiment_api_token=SecretStr("workflow-test-token")),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        workspace_manager=service.workspace,
        sandbox_executor=FakeExperimentSandbox(),
    )
    app.state.autonomous_service = service
    auth = {"Authorization": "Bearer workflow-test-token"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            payload = {"objective": "Compare models", "mode": "autonomous"}
            assert (await client.post("/agent/run", json=payload)).status_code == 401
            assert not provider.calls
            response = await client.post("/agent/run", json=payload, headers=auth)
            assert response.status_code == 200, response.text
            task_id = response.json()["task_id"]
            steps = await service.workflows.history(task_id, "steps")
            coding = next(item for item in steps if item["step_id"] == "code")
            paths = [
                f"/agent/tasks/{task_id}",
                f"/agent/tasks/{task_id}/workflow/plans",
                f"/agent/tasks/{task_id}/workflow/steps",
                f"/agent/tasks/{task_id}/workflow/reviews",
                f"/agent/tasks/{coding['child_task_id']}",
                f"/agent/tasks/{coding['child_task_id']}/evidence",
                f"/agent/tasks/{coding['child_task_id']}/workspace",
                f"/agent/tasks/{coding['child_task_id']}/workspace/files/source/train.py",
            ]
            for path in paths:
                assert (await client.get(path)).status_code == 401
                assert (await client.get(path, headers=auth)).status_code == 200
            rejected = await client.post(
                "/agent/run",
                json={
                    "objective": "Compare models",
                    "mode": "auto",
                    "budget": {"replans": 1},
                },
            )
            assert rejected.status_code == 422
    finally:
        await engine.dispose()


async def test_successful_attempt_artifacts_survive_later_replan(tmp_path: Path) -> None:
    responses = script(replan=True)
    responses[2] = call(
        "submit_review",
        {
            "status": "REPLAN",
            "issues": [
                {
                    "code": "repeat_requested",
                    "step_id": "train",
                    "message": "Repeat this engineering measurement once",
                }
            ],
            "suggested_steps": ["train"],
        },
    )
    service, engine = await workflow(
        tmp_path,
        ScriptedProvider(responses),
        FakeExperimentSandbox(),
    )
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        runs = [
            row
            for row in await service.workflows.history(result.task.id, "steps")
            if row["step_id"] == "train"
        ]
        assert len(runs) == 2
        assert len({row["child_task_id"] for row in runs}) == 2
        for row in runs:
            artifacts = await service.tasks.list_artifacts(row["child_task_id"])
            assert len(artifacts) == 10
            assert all(service.artifacts.read_verified(row["child_task_id"], a) for a in artifacts)
    finally:
        await engine.dispose()


class SlowSandbox(FakeExperimentSandbox):
    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if request.action == "run_python":
            await asyncio.sleep(3)
        return await super().run(request)


async def test_wall_timeout_finalizes_child_step_and_experiment(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, SlowSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(wall_seconds=2))
        assert result.task.status == "budget_exceeded"
        steps = await service.workflows.history(result.task.id, "steps")
        train = next(row for row in steps if row["step_id"] == "train")
        assert train["status"] == "failed"
        child = await service.tasks.get_task(train["child_task_id"])
        assert child and child.status == "failed"
        experiments = await service.tasks.list_experiments(train["child_task_id"])
        assert experiments and experiments[0].status == "failed"
        assert len(provider.calls) == 2
    finally:
        await engine.dispose()


async def test_missing_usage_stops_unmetered_model_calls(tmp_path: Path) -> None:
    responses = script()
    responses[0] = LLMResult(text="", model="no-usage", tool_calls=responses[0].tool_calls)
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "budget_exceeded"
        assert "provider_usage_missing" in (result.task.error_message or "")
        assert len(provider.calls) == 1
        assert not await service.workflows.history(result.task.id, "steps")
    finally:
        await engine.dispose()


async def test_code_replan_reruns_downstream_and_preserves_source_snapshot(tmp_path: Path) -> None:
    responses = [
        call("submit_task_plan", plan().model_dump()),
        call("create_file", {"path": "provenance.txt", "content": "first validated snapshot"}),
        call("submit_code_ready", {"summary": "Validated seeded comparison"}),
        call(
            "submit_review",
            {
                "status": "REPLAN",
                "issues": [
                    {
                        "code": "code_recheck",
                        "step_id": "code",
                        "message": "Revalidate sources",
                    }
                ],
            },
        ),
        call(
            "submit_plan_revision",
            {
                "plan": plan().model_dump(),
                "rerun_steps": ["code"],
                "reason": "Revalidate source and all dependent measurements",
            },
        ),
        call("submit_code_ready", {"summary": "Revalidated preserved comparison"}),
        call("submit_review", ReviewResult(status="PASS").model_dump()),
    ]
    service, engine = await workflow(
        tmp_path,
        ScriptedProvider(responses),
        FakeExperimentSandbox(),
    )
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        steps = await service.workflows.history(result.task.id, "steps")
        assert len(steps) == 4
        assert result.task.metrics["training_runs"] == 2
        for step in steps:
            assert (
                service.workspace.read_text(
                    step["child_task_id"],
                    "provenance.txt",
                )
                == "first validated snapshot"
            )
    finally:
        await engine.dispose()


async def test_replanner_cannot_drop_unresolved_original_work(tmp_path: Path) -> None:
    responses = script(replan=True)
    altered = plan().model_dump()
    altered["steps"][1]["id"] = "replacement"
    responses[3] = call(
        "submit_plan_revision",
        {
            "plan": altered,
            "rerun_steps": ["replacement"],
            "reason": "Attempt to hide the unresolved failed step",
        },
    )
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert len(await service.workflows.history(result.task.id, "plans")) == 1
        assert len(provider.calls) == 4
    finally:
        await engine.dispose()


async def test_auto_tool_choice_still_rejects_plain_text_plan(tmp_path: Path) -> None:
    provider = ScriptedProvider([response(text=plan().model_dump_json()) for _ in range(2)])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models")
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert len(provider.calls) == 2
        assert provider.calls[0]["tool_choice"] == "auto"
        assert not sandbox.requests
    finally:
        await engine.dispose()


async def test_workspace_tool_paths_round_trip_for_list_search_and_create(tmp_path: Path) -> None:
    workspace = WorkspaceManager(tmp_path)
    workspace.initialize("path-test")
    tools = {
        tool.definition.name: tool for tool in WorkspaceToolset(workspace, "path-test").tools()
    }
    created = await tools["create_file"].execute({"path": "nested/main.py", "content": "x = 1\n"})
    listed = await tools["list_files"].execute({})
    searched = await tools["search_code"].execute({"query": "x = 1"})
    paths = [
        json.loads(created.output)["path"],
        json.loads(listed.output)[0]["path"],
        json.loads(searched.output)[0]["path"],
    ]
    assert paths == ["nested/main.py"] * 3
    for path in paths:
        read = await tools["read_file"].execute({"path": path})
        assert read.succeeded
        assert json.loads(read.output)["content"] == "x = 1\n"


def test_dependency_context_is_bounded_without_mutating_authoritative_evidence() -> None:
    evidence = [
        {
            "reference": f"research-task:E{i}",
            "id": str(i),
            "claim": "c" * 2000,
            "excerpt": "e" * 8000,
        }
        for i in range(12)
    ]
    results: dict[str, dict[str, Any]] = {
        "research": {
            "child_task_id": "research-task",
            "status": "succeeded",
            "answer": "answer" * 10000,
            "evidence": evidence,
        },
        "code": {"child_task_id": "coding-task", "status": "succeeded", "source_sha256": "digest"},
    }
    compact = dependency_context(results)
    assert len(json.dumps(compact)) < 5000
    assert compact["research"]["evidence_total"] == 12
    assert compact["research"]["evidence"][0]["reference"] == "research-task:E0"
    assert compact["code"]["source_sha256"] == "digest"
    assert len(results["research"]["evidence"]) == 12
    assert len(evidence[0]["excerpt"]) == 8000


async def test_structured_protocol_retry_is_bounded_and_metered(tmp_path: Path) -> None:
    provider = ScriptedProvider([response(text="I will plan next."), *script()])
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded"
        assert result.task.metrics["model_calls"] == 4
        assert "Protocol correction:" in provider.calls[1]["messages"][-1].content
    finally:
        await engine.dispose()


async def test_artifact_contract_reaches_every_coordinator_and_coding_node(tmp_path: Path) -> None:
    provider = ScriptedProvider(script(replan=True))
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        for index in (0, 2, 3, 4):
            payload = json.loads(provider.calls[index]["messages"][1].content)
            assert payload["experiment_contract"] == experiment_contract()
        coding = json.loads(provider.calls[1]["messages"][1].content)
        assert json.dumps(experiment_contract(), ensure_ascii=False) in coding["objective"]
        schema = experiment_contract()["raw_metrics_schema"]
        assert schema["additionalProperties"] is False
        assert "primary_metric" not in schema["properties"]
    finally:
        await engine.dispose()


async def test_schema_correction_identifies_field_without_echoing_invalid_input(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call("submit_task_plan", {"goal": "Compare", "steps": "private-invalid-marker"}),
            *script(),
        ]
    )
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        correction = provider.calls[1]["messages"][-1].content
        assert "steps" in correction and "list_type" in correction
        assert "private-invalid-marker" not in correction
    finally:
        await engine.dispose()


async def test_malformed_native_arguments_retry_once_and_charge_usage(tmp_path: Path) -> None:
    from autoscholar.llm.errors import LLMResponseError
    from autoscholar.llm.models import TokenUsage

    class InvalidOnceProvider(ScriptedProvider):
        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            if not self.calls:
                self.calls.append({"messages": messages, **kwargs})
                raise LLMResponseError(
                    code="llm_invalid_tool_call",
                    message="Invalid arguments",
                    usage=TokenUsage(2, 5, 7),
                )
            return await super().generate(messages, **kwargs)

    provider = InvalidOnceProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        assert result.task.metrics["model_calls"] == 4
        assert result.task.metrics["total_tokens"] == 16
        assert "Protocol correction:" in provider.calls[1]["messages"][-1].content
    finally:
        await engine.dispose()


async def test_coding_successor_inherits_upstream_source_not_fresh_templates(
    tmp_path: Path,
) -> None:
    task_plan = plan().model_dump()
    task_plan["steps"].insert(
        1,
        {
            "id": "validate",
            "type": "coding",
            "description": "Validate upstream implementation",
            "expected_output": "Validated upstream source",
            "dependencies": ["code"],
        },
    )
    task_plan["steps"][-1]["dependencies"] = ["validate"]
    responses = [
        call("submit_task_plan", task_plan),
        call("create_file", {"path": "lineage.txt", "content": "upstream implementation"}),
        call("submit_code_ready", {"summary": "Implemented"}),
        call("submit_code_ready", {"summary": "Validated"}),
        call("submit_review", {"status": "PASS"}),
    ]
    service, engine = await workflow(
        tmp_path,
        ScriptedProvider(responses),
        FakeExperimentSandbox(),
    )
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        for step in await service.workflows.history(result.task.id, "steps"):
            assert (
                service.workspace.read_text(
                    step["child_task_id"],
                    "lineage.txt",
                )
                == "upstream implementation"
            )
    finally:
        await engine.dispose()


async def test_seeded_coding_uses_refreshed_snapshot_without_replaying_history(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call("submit_task_plan", plan().model_dump()),
            call("create_file", {"path": "marker.txt", "content": "before"}),
            call("edit_file", {"path": "marker.txt", "old_text": "before", "new_text": "after"}),
            call("submit_code_ready", {"summary": "Inspected current source"}),
            call("submit_review", {"status": "PASS"}),
        ]
    )
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        coding_calls = provider.calls[1:4]
        assert all(len(item["messages"]) == 2 for item in coding_calls)
        snapshots = [json.loads(item["messages"][-1].content) for item in coding_calls]
        assert "marker.txt" not in snapshots[0]["source_files"]
        assert snapshots[1]["source_files"]["marker.txt"] == "before"
        assert snapshots[2]["source_files"]["marker.txt"] == "after"
        assert all(
            {tool.name for tool in item["tools"]}
            == {"create_file", "edit_file", "submit_code_ready"}
            for item in coding_calls
        )
        assert [request.action for request in sandbox.requests[:2]] == [
            "static_check",
            "run_pytest",
        ]
    finally:
        await engine.dispose()


async def test_seeded_coding_edit_limit_never_auto_approves_unfinished_source(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            call("submit_task_plan", plan().model_dump()),
            *[
                call("create_file", {"path": f"note{i}.txt", "content": "pending"})
                for i in range(8)
            ],
            call("submit_review", {"status": "PASS"}),
        ]
    )
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models", budget=BudgetLimits(replans=0))
        assert result.task.status == "budget_exceeded"
        steps = await service.workflows.history(result.task.id, "steps")
        code = next(item for item in steps if item["step_id"] == "code")
        assert code["status"] == "failed"
        assert code["result"]["error_code"] == "coding_tool_budget_exceeded"
        assert not sandbox.requests
        assert len(provider.calls) == 10
    finally:
        await engine.dispose()


async def test_seeded_coding_batch_cannot_bypass_edit_limit(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            call("submit_task_plan", plan().model_dump()),
            LLMResult(
                text="",
                model="batched",
                usage=response().usage,
                tool_calls=tuple(
                    ToolCall(
                        id=f"call{i}",
                        name="create_file",
                        arguments={"path": f"note{i}.txt", "content": "pending"},
                    )
                    for i in range(9)
                ),
            ),
            call("submit_review", {"status": "PASS"}),
        ]
    )
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(replans=0))
        steps = await service.workflows.history(result.task.id, "steps")
        code = next(item for item in steps if item["step_id"] == "code")
        assert code["result"]["error_code"] == "coding_tool_budget_exceeded"
        snapshot = service.workspace.source_snapshot(code["child_task_id"])
        assert "note7.txt" in snapshot
        assert "note8.txt" not in snapshot
        assert len(provider.calls) == 3
    finally:
        await engine.dispose()


def test_plan_rejects_ambiguous_coding_source_merge() -> None:
    task_plan = plan().model_dump()
    task_plan["steps"].extend(
        [
            {"id": "other", "type": "coding", "description": "Other", "expected_output": "Code"},
            {
                "id": "merge",
                "type": "coding",
                "description": "Merge",
                "expected_output": "Code",
                "dependencies": ["code", "other"],
            },
        ]
    )
    with pytest.raises(ValueError, match="at most one coding predecessor"):
        TaskPlan.model_validate(task_plan)
