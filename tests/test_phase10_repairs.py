"""Free regressions for failures observed in the real Phase 10 pilot."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from autoscholar.agent.runner import AgentRunner
from autoscholar.coding.sandbox import SandboxRunRequest, SandboxRunResult
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import BudgetLimits
from autoscholar.llm.tool_arguments import decode_tool_arguments
from autoscholar.research.models import SearchResult
from tests.test_autonomous import InvalidOnceSandbox, call, script, workflow
from tests.test_research_agent import (
    ScriptedProvider,
    repository,
    search_services,
    successful_responses,
)


def test_tool_argument_parser_preserves_unicode_and_json_looking_string_values() -> None:
    expected = {"answer": '中文引号与括号 {}, 内容包含 "status": "PASS"', "x": 0.2}
    assert decode_tool_arguments(json.dumps(expected, ensure_ascii=False) + " \n") == expected


@pytest.mark.parametrize("repeat_invalid", [False, True])
async def test_artifact_recovery_refuses_unnecessary_coding_rerun(
    tmp_path: Path, repeat_invalid: bool
) -> None:
    responses = script(replan=True)
    invalid = call(
        "submit_plan_revision",
        {
            **responses[3].tool_calls[0].arguments,
            "rerun_steps": ["code", "train"],
        },
    )
    responses.insert(3, invalid)
    if repeat_invalid:
        responses[4] = invalid
    provider = ScriptedProvider(responses)
    sandbox = InvalidOnceSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        steps = await service.workflows.history(result.task.id, "steps")
        assert len([step for step in steps if step["step_id"] == "code"]) == 1
        assert result.task.metrics.get("code_repairs", 0) == 0
        constraint = json.loads(provider.calls[3]["messages"][1].content)["repair_constraints"]
        assert constraint == {
            "experiment_only": True,
            "rerun_steps": ["train"],
            "preserve_entire_plan": True,
        }
        assert "Artifact-only recovery" in provider.calls[4]["messages"][-1].content
        if repeat_invalid:
            assert result.task.status == "failed"
            assert result.task.error_code == "autonomous_protocol_invalid"
            assert result.task.metrics["training_runs"] == 1
            assert len(provider.calls) == 5
        else:
            assert result.task.status == "succeeded", result.task.error_message
            assert result.task.metrics["training_runs"] == 2
            training = [item for item in sandbox.requests if item.action == "run_python"]
            assert len(training) == 2 and training[0].files == training[1].files
            assert result.task.metrics["model_calls"] == 6
    finally:
        await engine.dispose()


async def test_experiment_retry_rejects_changed_validated_source(tmp_path: Path) -> None:
    class TamperedSourceSandbox(InvalidOnceSandbox):
        workspace: WorkspaceManager | None = None

        async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
            result = await super().run(request)
            if request.action == "run_python":
                assert self.workspace is not None
                code_id = self.requests[0].task_id
                original = self.workspace.read_text(code_id, "train.py")
                self.workspace.write_text(
                    code_id, "train.py", original + "\n# modified\n", overwrite=True
                )
            return result

    provider = ScriptedProvider(script(replan=True))
    sandbox = TamperedSourceSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    sandbox.workspace = service.workspace
    try:
        result = await service.run("Compare models")
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert "source changed" in (result.task.error_message or "")
        assert result.task.metrics["training_runs"] == 1
        assert len(provider.calls) == 3
    finally:
        await engine.dispose()


@pytest.mark.parametrize("same_topic_different_source", [False, True])
async def test_source_deduplication_retains_each_query_topic(
    same_topic_different_source: bool,
) -> None:
    store, engine = await repository()
    services = search_services()
    paper = services[1]
    shared = SearchResult(
        source_type="paper",
        provider="semantic_scholar",
        title="Adapter methods",
        url="https://example.org/adapters",
        external_id="shared",
        content=(
            "LoRA adds trainable low-rank matrices. QLoRA trains adapters through a 4-bit model."
        ),
    )
    paper.results = dict.fromkeys(paper.results, shared)
    responses = successful_responses()
    if same_topic_different_source:
        query = responses[1].tool_calls[0].arguments["queries"][1]
        query.update(topic="LoRA", source_type="web")
        services[0].results[query["query"]] = replace(shared, source_type="web", provider="tavily")
    provider = ScriptedProvider(responses)
    runner = AgentRunner(provider=provider, repository=store, tools=[], research_services=services)
    try:
        result = await runner.run("Compare LoRA, QLoRA and DoRA", mode="research")
        assert result.task.status == "succeeded", result.task.warnings
        expected = {"LoRA", "DoRA"} if same_topic_different_source else {"LoRA", "QLoRA", "DoRA"}
        assert {item.topic for item in result.task.evidence} == expected
        assert {item.source_type for item in result.task.evidence} == {"paper", "web"}
        assert len(result.task.evidence) == 3
        assert len({item.url for item in result.task.evidence}) == 2
    finally:
        await engine.dispose()


async def test_missing_topic_feedback_names_available_candidate_without_auto_selecting() -> None:
    store, engine = await repository()
    responses = successful_responses()
    arguments = responses[2].tool_calls[0].arguments
    incomplete = call("submit_evidence", {"items": [arguments["items"][0], arguments["items"][2]]})
    responses.insert(2, incomplete)
    provider = ScriptedProvider(responses)
    runner = AgentRunner(
        provider=provider, repository=store, tools=[], research_services=search_services()
    )
    try:
        result = await runner.run("Compare LoRA, QLoRA and DoRA", mode="research")
        assert result.task.status == "succeeded"
        feedback = provider.calls[3]["messages"][-1].content
        assert 'available_candidate_ids_by_topic:{"QLoRA": ["C2"]}' in feedback
        assert len(provider.calls) == 5
        assert len(result.task.evidence) == 3
        assert "separately retrieves local project documents" in (
            provider.calls[1]["messages"][0].content
        )
    finally:
        await engine.dispose()
