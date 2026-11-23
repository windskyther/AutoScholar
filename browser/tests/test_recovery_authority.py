"""Free regressions derived from v5; no saved private run data or vendor requests."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autoscholar.agent.records import AgentTaskRecord, ExperimentRecord
from autoscholar.core.budget import BudgetLimits
from autoscholar.orchestration.contracts import review_plan, stage_contract
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.models import ReviewIssue
from autoscholar.orchestration.review_facts import requirement_issue_error
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import InvalidOnceSandbox, call, script, workflow
from tests.test_durable_workflow import drain
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_workflow_foundation import plan


async def test_restart_after_narrow_revision_preserves_source_and_retry_count(
    tmp_path: Path,
) -> None:
    responses = script(replan=True)
    responses[2] = call(
        "submit_review",
        {
            "status": "REPLAN",
            "issues": [
                {
                    "step_id": "train",
                    "code": "descriptive_alias",
                    "check": "raw_metrics",
                    "message": "Collected metrics invalid",
                    "requirement_id": "experiment.raw_metrics",
                }
            ],
            "suggested_steps": ["train"],
        },
    )
    provider = ScriptedProvider(responses)
    sandbox = InvalidOnceSandbox()
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"
    service, engine = await workflow(
        tmp_path / "workspace", provider, sandbox, database_url=database_url
    )
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit(
            {"objective": "Compare", "budget": {"code_repairs": 0}}, "restart"
        )
        for _ in range(5):
            assert await durable.tick(task_id)
        before, job = await durable.repository.snapshot(task_id)
        assert before.version == 2 and set(before.results) == {"code"}
        assert before.review and before.review.issues[0].requirement_id == "experiment.raw_metrics"
        assert job.usage["training_runs"] == job.usage["replans"] == 1 and not job.pending_calls
    finally:
        await engine.dispose()
    service, engine = await workflow(
        tmp_path / "workspace", provider, sandbox, database_url=database_url
    )
    try:
        restarted = DurableService(service)
        assert await drain(restarted, task_id) == "succeeded"
        after, job = await restarted.repository.snapshot(task_id)
        assert after.sources == before.sources and after.specification == before.specification
        assert job.usage["training_runs"] == 2 and job.usage["replans"] == 1
        assert len(provider.calls) == 5 and not job.pending_calls
        steps = await service.workflows.history(task_id, "steps")
        assert [s["step_id"] for s in steps].count("code") == 1
        training = [r for r in sandbox.requests if r.action == "run_python"]
        assert training[0].files == training[1].files
    finally:
        await engine.dispose()


@pytest.mark.parametrize("changed", ["specification", "source", "training_trace"])
async def test_changed_durable_experiment_facts_prevent_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    provider = ScriptedProvider(script(replan=True))
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    list_experiments = service.tasks.list_experiments
    get_task = service.tasks.get_task

    async def records(task_id: str) -> list[ExperimentRecord]:
        saved = await list_experiments(task_id)
        if saved and changed == "specification":
            saved[-1] = replace(saved[-1], specification={**saved[-1].specification, "seed": 99})
        elif saved and changed == "source":
            saved[-1] = replace(saved[-1], source_sha256="0" * 64)
        return saved

    async def child(task_id: str) -> AgentTaskRecord | None:
        saved = await get_task(task_id)
        if saved and changed == "training_trace":
            saved = replace(
                saved,
                tool_calls=[
                    replace(t, output=json.dumps({"status": "failed", "exit_code": 1}))
                    if t.tool_name == "run_python"
                    else t
                    for t in saved.tool_calls
                ],
            )
        return saved

    monkeypatch.setattr(service.tasks, "list_experiments", records)
    monkeypatch.setattr(service.tasks, "get_task", child)
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        expected = {
            "specification": "specification changed",
            "source": "source lineage changed",
            "training_trace": "successful saved training trace",
        }
        assert expected[changed] in (result.task.error_message or "")
        assert len(provider.calls) == 2 and result.task.metrics["training_runs"] == 1
        assert len(await service.workflows.history(result.task.id, "plans")) == 1
    finally:
        await engine.dispose()


def unsupported_coding_diagnosis() -> dict[str, Any]:
    # Minimal public regression derived from the real review, not a copied raw receipt.
    return {
        "status": "REPLAN",
        "issues": [
            {
                "step_id": "train",
                "code": "raw_metrics_schema_mismatch",
                "check": "raw_metrics",
                "message": "Collected raw metrics failed schema validation",
            },
            {
                "step_id": "code",
                "code": "missing_schema_validation_of_raw_metrics",
                "check": "requirement",
                "message": "Repeat coding to validate training artifacts",
            },
            {
                "step_id": "train",
                "code": "rerun_after_concrete_metrics_fix",
                "check": "requirement",
                "message": "Retry the experiment",
            },
        ],
        "suggested_steps": ["code", "train"],
    }


async def test_v5_requirement_diagnosis_is_corrected_then_only_experiment_retries(
    tmp_path: Path,
) -> None:
    responses = script(replan=True)
    responses.insert(2, call("submit_review", unsupported_coding_diagnosis()))
    provider = ScriptedProvider(responses)
    sandbox = InvalidOnceSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "succeeded", result.task.error_message
        assert len(provider.calls) == result.task.metrics["model_calls"] == 6
        feedback = provider.calls[3]["messages"][-1].content
        assert "source_path" in feedback and "source_quote" in feedback
        review_input = json.loads(provider.calls[2]["messages"][1].content)
        assert review_input["collection_retry_candidates"] == ["train"]
        retry = json.loads(provider.calls[4]["messages"][1].content)
        assert retry["repair_constraints"] == {
            "experiment_only": True,
            "rerun_steps": ["train"],
            "preserve_entire_plan": True,
        }
        assert "plan" not in provider.calls[4]["tools"][0].parameters["properties"]
        steps = await service.workflows.history(result.task.id, "steps")
        assert [s["step_id"] for s in steps].count("code") == 1
        assert result.task.metrics["training_runs"] == 2
        training = [r for r in sandbox.requests if r.action == "run_python"]
        assert training[0].files == training[1].files
    finally:
        await engine.dispose()


@pytest.mark.parametrize("alias", ["raw_metrics_schema_mismatch", "new_code_42", "MISSING_JSON"])
@pytest.mark.parametrize("check", ["requirement", "raw_metrics", "artifacts"])
async def test_descriptive_issue_names_do_not_change_machine_retry_eligibility(
    tmp_path: Path,
    alias: str,
    check: str,
) -> None:
    responses = script(replan=True)
    responses[2] = call(
        "submit_review",
        {
            "status": "REPLAN",
            "issues": [
                {"step_id": "train", "code": alias, "check": check, "message": "Retry collection"}
            ],
            "suggested_steps": ["train"],
        },
    )
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "succeeded", result.task.error_message
        assert result.task.metrics["training_runs"] == 2 and len(provider.calls) == 5
        payload = json.loads(provider.calls[3]["messages"][1].content)
        assert payload["repair_constraints"]["experiment_only"] is True
    finally:
        await engine.dispose()


async def test_repeated_unproven_requirement_is_terminal_without_automatic_pass(
    tmp_path: Path,
) -> None:
    bad = call("submit_review", unsupported_coding_diagnosis())
    provider = ScriptedProvider([*script()[:2], bad, bad])
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "failed"
        assert result.task.error_code == "autonomous_protocol_invalid"
        assert len(provider.calls) == 4 and result.task.metrics["training_runs"] == 1
        assert len(await service.workflows.history(result.task.id, "plans")) == 1
        assert not await service.workflows.history(result.task.id, "reviews")
    finally:
        await engine.dispose()


async def test_planner_training_output_does_not_become_a_coding_review_requirement(
    tmp_path: Path,
) -> None:
    proposed = plan()
    conflicting = "A validated CPU training run with all collected checkpoints and raw metrics"
    proposed.steps[0].expected_output = conflicting
    original = proposed.model_dump()
    assert (
        review_plan(proposed)["steps"][0]["expected_output"]
        == stage_contract("coding")["required_output"]
    )
    assert proposed.model_dump() == original
    responses = script()
    responses[0] = call("submit_task_plan", original)
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models")
        assert result.task.status == "succeeded"
        payload = json.loads(provider.calls[2]["messages"][1].content)
        assert payload["plan"]["steps"][0]["expected_output"] != conflicting
        history = await service.workflows.history(result.task.id, "plans")
        assert history[0]["payload"]["steps"][0]["expected_output"] == conflicting
        assert "stage_contracts" in json.loads(provider.calls[0]["messages"][1].content)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("overrides", "accepted"),
    [
        ({}, False),
        ({"requirement_id": "invented"}, False),
        ({"requirement_id": "experiment.raw_metrics"}, False),
        ({"requirement_quote": "Invented requirement"}, False),
        ({"requirement_quote": " "}, False),
        ({"requirement_id": "coding.source_contract"}, True),
        ({"requirement_quote": "write the required provenance file"}, True),
    ],
)
def test_coding_claims_bind_a_stage_requirement_or_actual_objective(
    overrides: dict[str, Any],
    accepted: bool,
) -> None:
    issue = ReviewIssue.model_validate(
        {
            "code": "arbitrary_label",
            "step_id": "code",
            "message": "Wrong source content",
            "check": "requirement",
            "source_path": "provenance.txt",
            "source_quote": "wrong",
            **overrides,
        }
    )
    facts = {
        "step_type": "coding",
        "checks": {"coding_validation": True},
        "source_excerpts": {"provenance.txt": "wrong"},
    }
    reason = requirement_issue_error(
        issue, facts, "Compare models and write the required provenance file"
    )
    assert (reason is None) is accepted


async def test_genuine_code_issue_with_zero_budget_stops_before_replan_or_new_child(
    tmp_path: Path,
) -> None:
    objective = "Compare models and write provenance.txt containing exactly verified"
    responses = [
        script()[0],
        call("create_file", {"path": "provenance.txt", "content": "unverified"}),
        script()[1],
        call(
            "submit_review",
            {
                "status": "REPLAN",
                "suggested_steps": ["code"],
                "issues": [
                    {
                        "code": "incorrect_provenance",
                        "step_id": "code",
                        "check": "requirement",
                        "message": (
                            "The requested exact text is verified, but source contains unverified"
                        ),
                        "source_path": "provenance.txt",
                        "source_quote": "unverified",
                        "requirement_quote": "write provenance.txt containing exactly verified",
                    }
                ],
            },
        ),
    ]
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run(objective, budget=BudgetLimits(code_repairs=0))
        assert result.task.status == "budget_exceeded"
        assert "code_repairs" in (result.task.error_message or "")
        assert len(provider.calls) == 4 and result.task.metrics.get("replans", 0) == 0
        assert len(await service.workflows.history(result.task.id, "steps")) == 2
        assert len(await service.workflows.history(result.task.id, "plans")) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("replans", [1, 3])
async def test_second_collection_failure_cannot_loop_a_third_training(
    tmp_path: Path, replans: int
) -> None:
    from tests.test_autonomous import InvalidOnceSandbox as Once

    class AlwaysInvalid(Once):
        async def run(self, request: Any) -> Any:
            if request.action == "run_python":
                self.failed = False
            return await super().run(request)

    responses = script(replan=True)
    responses[-1] = responses[2]  # Final reviewer must retain the second deterministic failure.
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, AlwaysInvalid())
    try:
        result = await service.run(
            "Compare models", budget=BudgetLimits(replans=replans, code_repairs=0)
        )
        assert result.task.status == "failed"
        assert "retry exhausted" in (result.task.error_message or "")
        assert result.task.metrics["training_runs"] == 2 and len(provider.calls) == 5
    finally:
        await engine.dispose()


async def test_supported_code_fix_updates_source_and_remeasures_downstream(tmp_path: Path) -> None:
    goal = "Compare models and write provenance.txt containing exactly verified"
    review = {
        "status": "REPLAN",
        "suggested_steps": ["code"],
        "issues": [
            {
                "code": "wrong_provenance",
                "step_id": "code",
                "check": "requirement",
                "message": "The requested exact file text is wrong",
                "source_path": "provenance.txt",
                "source_quote": "unverified",
                "requirement_quote": "write provenance.txt containing exactly verified",
            }
        ],
    }
    responses = [
        script()[0],
        call("create_file", {"path": "provenance.txt", "content": "unverified"}),
        script()[1],
        call("submit_review", review),
        call(
            "submit_plan_revision",
            {
                "plan": plan().model_dump(),
                "rerun_steps": ["code"],
                "reason": "Fix the evidenced user requirement and remeasure dependent training",
            },
        ),
        call(
            "edit_file",
            {"path": "provenance.txt", "old_text": "unverified", "new_text": "verified"},
        ),
        script()[1],
        script()[2],
    ]
    sandbox = FakeExperimentSandbox()
    provider = ScriptedProvider(responses)
    service, engine = await workflow(tmp_path, provider, sandbox)
    try:
        result = await service.run(goal, budget=BudgetLimits(code_repairs=1))
        assert result.task.status == "succeeded", result.task.error_message
        assert (
            result.task.metrics["code_repairs"] == 1 and result.task.metrics["training_runs"] == 2
        )
        training = [r for r in sandbox.requests if r.action == "run_python"]
        assert training[0].files["provenance.txt"] == "unverified"
        assert training[1].files["provenance.txt"] == "verified"
    finally:
        await engine.dispose()
