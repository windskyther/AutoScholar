"""Free regressions for v3 Reviewer false positives and complete revision validation."""

import json
from pathlib import Path
from typing import Any

import pytest

from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import BudgetLimits
from autoscholar.llm import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from autoscholar.orchestration.models import ExperimentRetry, ReviewIssue
from autoscholar.orchestration.review_facts import contradicted_issue, unsupported_source_issue
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import InvalidOnceSandbox, call, script, workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_workflow_foundation import plan


@pytest.mark.parametrize(
    "code,step,check",
    [
        ("MISSING_ARTIFACT_CONFIRMATION", "train", "requirement"),
        ("RAW_METRICS_SCHEMA_RISK", "train", "requirement"),
        ("CODING_VALIDATION_EVIDENCE_MISSING", "code", "requirement"),
        ("arbitrary_code", "code", "coding_validation"),
        ("arbitrary_code", "train", "raw_metrics"),
    ],
)
async def test_verified_positive_facts_correct_reviewer_without_rerunning_work(
    tmp_path: Path,
    code: str,
    step: str,
    check: str,
) -> None:
    responses = script()
    responses.insert(
        2,
        call(
            "submit_review",
            {
                "status": "REPLAN",
                "issues": [
                    {
                        "code": code,
                        "step_id": step,
                        "message": "Missing validation evidence",
                        "check": check,
                    }
                ],
                "suggested_steps": [step],
            },
        ),
    )
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "succeeded", result.task.error_message
        assert len(provider.calls) == 4
        assert "contradicts verified facts" in provider.calls[3]["messages"][-1].content
        payload = json.loads(provider.calls[2]["messages"][1].content)
        facts = payload["verified_checks"]
        assert facts["code"]["checks"]["coding_validation"]
        assert {t["tool"] for t in facts["code"]["validation_traces"]} == {
            "static_check",
            "run_pytest",
        }
        assert len(facts["train"]["artifact_manifest"]) == 10
        assert facts["train"]["checks"] == {
            "artifacts": True,
            "raw_metrics": True,
            "source_handoff": True,
        }
        assert "winner" in facts["train"]["summary_only_keys"]
        assert "winner" not in facts["train"]["raw_metric_keys"]
        assert result.task.metrics["training_runs"] == 1
        assert result.task.metrics.get("replans", 0) == 0
        steps = await service.workflows.history(result.task.id, "steps")
        assert len(steps) == 2
    finally:
        await engine.dispose()


async def test_repeated_false_review_stops_instead_of_silently_passing(tmp_path: Path) -> None:
    false = call(
        "submit_review",
        {
            "status": "REPLAN",
            "issues": [
                {
                    "code": "MISSING_ARTIFACT_CONFIRMATION",
                    "step_id": "train",
                    "message": "Missing files",
                }
            ],
        },
    )
    provider = ScriptedProvider([*script()[:2], false, false])
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert len(provider.calls) == 4
        assert not await service.workflows.history(result.task.id, "reviews")
        assert result.task.metrics["training_runs"] == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bad", ["full_plan", "nested_fields", "duplicates", "coding"])
async def test_artifact_retry_schema_cannot_mutate_or_resubmit_plan(
    tmp_path: Path,
    bad: str,
) -> None:
    responses = script(replan=True)
    valid = responses[3]
    arguments = dict(valid.tool_calls[0].arguments)
    if bad == "full_plan":
        arguments["plan"] = plan().model_dump()
    elif bad == "nested_fields":
        arguments = {"plan": arguments}
    else:
        arguments["rerun_steps"] = ["train", "train"] if bad == "duplicates" else ["code"]
    responses.insert(3, call("submit_plan_revision", arguments))
    provider = ScriptedProvider(responses)
    sandbox = InvalidOnceSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "succeeded", result.task.error_message
        schema = provider.calls[3]["tools"][0].parameters
        assert set(schema["properties"]) == {"rerun_steps", "reason"}
        assert schema["additionalProperties"] is False
        assert result.task.metrics["training_runs"] == 2
        assert result.task.metrics.get("code_repairs", 0) == 0
        histories = await service.workflows.history(result.task.id, "plans")
        assert len(histories) == 2 and histories[0]["payload"] == histories[1]["payload"]
        training = [r for r in sandbox.requests if r.action == "run_python"]
        assert training[0].files == training[1].files
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bad", ["uncovered_issue", "changed_goal", "duplicate_reruns"])
async def test_full_revision_semantics_are_corrected_before_any_state_mutation(
    tmp_path: Path,
    bad: str,
) -> None:
    review = call(
        "submit_review",
        {
            "status": "REPLAN",
            "issues": [
                {
                    "code": "requirement_missing",
                    "step_id": "code",
                    "message": "Revalidate the requirement",
                    "requirement_id": "coding.source_contract",
                    "source_path": "train.py",
                    "source_quote": "class MLP(nn.Module):",
                }
            ],
        },
    )
    revision: dict[str, Any] = {
        "plan": plan().model_dump(),
        "rerun_steps": ["code"],
        "reason": "Revalidate requirement",
    }
    invalid = json.loads(json.dumps(revision))
    if bad == "uncovered_issue":
        invalid["rerun_steps"] = ["train"]
    elif bad == "changed_goal":
        invalid["plan"]["goal"] = "Changed goal"
    else:
        invalid["rerun_steps"] = ["code", "code"]
    provider = ScriptedProvider(
        [
            *script()[:2],
            review,
            call("submit_plan_revision", invalid),
            call("submit_plan_revision", revision),
            script()[1],
            script()[2],
        ]
    )
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded", result.task.error_message
        assert len(provider.calls) == 7
        assert "Protocol correction:" in provider.calls[4]["messages"][-1].content
        assert len(await service.workflows.history(result.task.id, "plans")) == 2
        assert result.task.metrics["training_runs"] == 2
        assert result.task.metrics["code_repairs"] == 1
    finally:
        await engine.dispose()


def test_review_contradiction_requires_explicit_verified_true() -> None:
    issue = ReviewIssue(code="any", step_id="train", message="Missing artifact", check="artifacts")
    assert contradicted_issue(issue, {"checks": {"artifacts": True}})
    for value in (False, None, "true", 1):
        assert not contradicted_issue(issue, {"checks": {"artifacts": value}})
    assert not contradicted_issue(issue, {})


@pytest.mark.parametrize(
    "path,quote,unsupported",
    [
        (None, None, True),
        ("other.py", "x = 1", True),
        ("train.py", "fabricated", True),
        ("train.py", "   ", True),
        ("train.py", "x = 1", False),
    ],
)
def test_source_defect_requires_a_current_exact_source_quote(
    path: str | None,
    quote: str | None,
    unsupported: bool,
) -> None:
    issue = ReviewIssue(
        code="coding_handoff_schema_unverified",
        step_id="code",
        message="Source bug",
        source_path=path,
        source_quote=quote,
    )
    assert (
        unsupported_source_issue(issue, {"source_excerpts": {"train.py": "x = 1\n"}}) is unsupported
    )


def test_narrow_retry_forbids_legacy_full_dag() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExperimentRetry.model_validate(
            {"plan": plan().model_dump(), "rerun_steps": ["train"], "reason": "Retry"}
        )


async def test_unsupported_code_diagnosis_is_corrected_then_experiment_only_recovers(
    tmp_path: Path,
) -> None:
    responses = script(replan=True)
    responses.insert(
        2,
        call(
            "submit_review",
            {
                "status": "REPLAN",
                "issues": [
                    {
                        "code": "coding_handoff_schema_unverified",
                        "step_id": "code",
                        "message": "Invalid collected metrics prove a source bug",
                    }
                ],
            },
        ),
    )
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "succeeded", result.task.error_message
        assert "exact nonblank source_quote" in provider.calls[3]["messages"][-1].content
        assert result.task.metrics["training_runs"] == 2
        assert len(provider.calls) == 6
        assert (
            len(
                [
                    s
                    for s in await service.workflows.history(result.task.id, "steps")
                    if s["step_id"] == "code"
                ]
            )
            == 1
        )
    finally:
        await engine.dispose()


async def test_source_tampering_during_narrow_retry_wait_stops_before_second_training(
    tmp_path: Path,
) -> None:
    sandbox = InvalidOnceSandbox()

    class TamperingProvider(ScriptedProvider):
        workspace: WorkspaceManager | None = None

        async def generate(
            self,
            messages: list[ConversationMessage],
            *,
            tools: list[ToolDefinition] | None = None,
            tool_choice: ToolChoice = "none",
        ) -> LLMResult:
            result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
            if tools and tools[0].name == "submit_plan_revision":
                assert self.workspace is not None
                task_id = sandbox.requests[0].task_id
                source = self.workspace.read_text(task_id, "train.py")
                self.workspace.write_text(
                    task_id, "train.py", source + "\n# tampered\n", overwrite=True
                )
            return result

    provider = TamperingProvider(script(replan=True))
    service, engine = await workflow(tmp_path, provider, sandbox)
    provider.workspace = service.workspace
    try:
        result = await service.run("Compare models")
        assert result.task.status == "failed"
        assert "source changed" in (result.task.error_message or "")
        assert len(provider.calls) == 4
        assert result.task.metrics["training_runs"] == 1
        assert len(await service.workflows.history(result.task.id, "plans")) == 1
    finally:
        await engine.dispose()
