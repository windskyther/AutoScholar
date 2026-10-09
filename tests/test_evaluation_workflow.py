"""E2E control-flow/scorer tests with explicit fake execution; real Docker is run separately."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import create_async_engine

from autoscholar.agent.database_models import TaskReviewRow
from autoscholar.coding.sandbox import SandboxHealth, SandboxRunRequest, SandboxRunResult
from autoscholar.core.budget import current_budget
from autoscholar.core.journal import current_journal
from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.e2e_adapter import (
    InjectedWorkflowAdapter,
    WorkflowLabels,
    WorkflowObservation,
    score_workflow,
)
from autoscholar.evaluation.models import Observation, RunConfiguration
from autoscholar.evaluation.runner import run_evaluation
from autoscholar.evaluation.workflow_fixture import WorkflowFixture, WorkflowLimits
from autoscholar.evaluation.workflow_runtime import local_workflow
from tests.test_evaluation_execution import collected
from tests.test_experiment_service import FakeExperimentSandbox

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "benchmarks/end_to_end"
RESOURCES = {"sandbox_image": "b" * 64, "dataset": "a" * 64}


class WorkflowReplaySandbox(FakeExperimentSandbox):
    """Fake training/grade bytes. Does NOT execute sources, tensors, neural training or Docker."""

    async def health(self) -> SandboxHealth:
        return (await super().health()).model_copy(update={"image_sha256": "b" * 64})

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if request.path != "eval_oracle.py":
            return await super().run(request)
        self.requests.append(request)
        payload = json.loads(request.files["oracle_payload.json"])
        return collected(
            {
                "schema_version": 1,
                "dataset_verified": True,
                "plots_verified": True,
                "models": [
                    {
                        "model": model,
                        "accuracy": accuracy,
                        "parameters": 100,
                        "verified": payload["raw_metrics"]["runs"][index]["test_accuracy"]
                        == accuracy,
                    }
                    for index, (model, accuracy) in enumerate((("mlp", 0.7), ("cnn", 0.8)))
                ],
            }
        )


def adapter(
    tmp_path: Path, sandbox: WorkflowReplaySandbox | None = None
) -> InjectedWorkflowAdapter:
    fixture, digest = load_fixture(PUBLIC / "workflow_fixture_v1.json", WorkflowFixture)
    return InjectedWorkflowAdapter(
        fixture,
        fixture_sha256=digest,
        sandbox=sandbox or WorkflowReplaySandbox(),
        resources=RESOURCES,
        workspace_root=tmp_path,
    )


@pytest.mark.parametrize("index", range(5))
async def test_real_workflow_control_flow_with_explicit_replay(tmp_path: Path, index: int) -> None:
    suite, _ = load_suite(PUBLIC / "public_v1.json")
    sandbox = WorkflowReplaySandbox()
    injected = adapter(tmp_path, sandbox)
    case = suite.cases[index]
    injected.validate_case(case)
    observation = await injected.execute(case.prompt, case.inputs, seed=42)
    score = injected.score(observation, case.expected)
    assert all(score.checks.values()), score.checks
    assert score.task_success == case.expected["task_success"]
    assert observation.usage.external_api_calls == observation.usage.total_tokens == 0
    assert observation.usage.budget_tokens == 3 * (observation.usage.model_calls or 0)
    assert observation.usage.monetary_cost is None
    assert current_budget.get() is None and current_journal.get() is None
    requests = sandbox.requests
    assert not any(".env" in path for request in requests for path in request.files)
    oracle = [request for request in requests if request.path == "eval_oracle.py"]
    assert len(oracle) == (0 if index == 3 else 1)
    assert all(
        "eval_oracle.py" not in request.files
        for request in requests
        if request.path != "eval_oracle.py"
    )
    assert "expected" not in observation.payload and "answer" not in observation.payload
    if index == 3:
        assert not requests and observation.usage.model_calls == 1
    if index == 4:
        assert observation.payload["workflow_status"] == "succeeded"
        assert score.task_success is False and score.metrics["checkpoint_correctness"] == 0


async def test_replan_overrides_scripted_pass_and_keeps_successful_predecessors(
    tmp_path: Path,
) -> None:
    suite, _ = load_suite(PUBLIC / "public_v1.json")
    case = suite.cases[2]
    observation = await adapter(tmp_path).execute(case.prompt, case.inputs, seed=42)
    assert observation.payload["step_runs"] == {"research": 1, "code": 1, "train": 2}
    database = next(tmp_path.glob("evale-*/workflow.sqlite"))
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(database)))
    try:
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(TaskReviewRow.payload).order_by(TaskReviewRow.plan_version)
                    )
                )
                .scalars()
                .all()
            )
        assert rows[0]["status"] == "REPLAN"
        assert rows[0]["issues"][0]["code"] == "experiment_metrics_invalid"
        assert rows[1]["status"] == "PASS"
    finally:
        await engine.dispose()


def completed_payload() -> dict[str, Any]:
    return {
        "workflow_status": "succeeded",
        "step_runs": {"research": 1, "code": 1, "train": 1},
        "replans": 0,
        "training_runs": 1,
        "restarts": 0,
        "checkpoints": 7,
        "trace_bound": True,
        "steps_complete": True,
        "usage_consistent": True,
        "code_checks_passed": True,
        "evidence_bound": True,
        "source_handoff": True,
        "report_published": True,
        "report_bound": True,
        "latest_results_only": True,
        "execution_sandbox_runs": 4,
        "grading_sandbox_runs": 1,
        "verification": {
            "metric_extraction_valid": True,
            "artifact_set_valid": True,
            "artifact_integrity_valid": True,
            "artifact_count": 10,
            "verified_artifact_count": 10,
            "oracle": {
                "schema_version": 1,
                "dataset_verified": True,
                "plots_verified": True,
                "models": [
                    {"model": model, "accuracy": 0.5, "parameters": 100, "verified": True}
                    for model in ("mlp", "cnn")
                ],
            },
        },
    }


@pytest.mark.parametrize(
    "defect",
    [
        "trace_bound",
        "steps_complete",
        "usage_consistent",
        "code_checks_passed",
        "evidence_bound",
        "source_handoff",
        "report_published",
        "report_bound",
        "latest_results_only",
        "oracle_missing",
        "checkpoint_failed",
        "physical_integrity",
        "metric_extraction",
        "dataset",
        "plots",
        "resume",
        "artifact_count",
        "verified_count",
        "grading_missing",
    ],
)
def test_successful_status_alone_cannot_earn_task_success(defect: str) -> None:
    suite, _ = load_suite(PUBLIC / "public_v1.json")
    payload = completed_payload()
    if defect == "oracle_missing":
        payload["verification"]["oracle"] = None
    elif defect == "checkpoint_failed":
        payload["verification"]["oracle"]["models"][0]["verified"] = False
    elif defect == "physical_integrity":
        payload["verification"]["artifact_integrity_valid"] = False
    elif defect == "metric_extraction":
        payload["verification"]["metric_extraction_valid"] = False
    elif defect in ("dataset", "plots"):
        payload["verification"]["oracle"][defect + "_verified"] = False
    elif defect == "resume":
        payload["restarts"] = 1
        payload["resume_verified"] = False
    elif defect == "artifact_count":
        payload["verification"]["artifact_count"] = 0
    elif defect == "verified_count":
        payload["verification"]["verified_artifact_count"] = 9
    elif defect == "grading_missing":
        payload["grading_sandbox_runs"] = 0
    else:
        payload[defect] = False
    result = score_workflow(Observation(payload=payload), suite.cases[0].expected)
    assert result.task_success is False
    assert not all(result.checks.values())


def test_missing_oracle_is_not_correctly_detected_deception() -> None:
    suite, _ = load_suite(PUBLIC / "public_v1.json")
    payload = completed_payload()
    payload["verification"]["oracle"] = None
    result = score_workflow(Observation(payload=payload), suite.cases[4].expected)
    assert result.task_success is False
    assert result.checks["independent_verification"] is False
    assert result.metrics["checkpoint_correctness"] is None


def test_gold_labels_do_not_define_actual_task_success() -> None:
    suite, _ = load_suite(PUBLIC / "public_v1.json")
    result = score_workflow(Observation(payload=completed_payload()), suite.cases[4].expected)
    assert result.task_success is True
    assert result.checks["task_outcome"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"model_calls": True},
        {"model_calls": 41},
        {"training_runs": 3},
        {"replans": 2},
        {"wall_seconds": 601},
        {"total_tokens": 1001},
        {"sandbox_runs": 17},
    ],
)
def test_workflow_fixture_limits_are_strict_and_bounded(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        WorkflowLimits.model_validate(change)


@pytest.mark.parametrize(
    "category_args",
    [
        ["--profile", "offline"],
        ["--profile", "injected"],
        ["--profile", "injected", "--sandbox-container", "autoscholar-sandbox-manager-1"],
        [
            "--profile",
            "injected",
            "--sandbox-container",
            "autoscholar-eval-unit-sandbox-manager-1",
            "--oracle",
            "bad.json",
        ],
    ],
)
def test_e2e_cli_rejects_implicit_execution_and_custom_oracle(
    monkeypatch: pytest.MonkeyPatch, category_args: list[str]
) -> None:
    import sys

    monkeypatch.setattr(sys, "argv", ["eval", "run", "--category", "end_to_end", *category_args])
    assert cli.main() == 2


async def test_runtime_reopen_reads_committed_budget_and_disposes_on_cancel(tmp_path: Path) -> None:
    async with local_workflow(tmp_path) as state:
        await state.tasks.create_task(task_id="public", objective="public", mode="autonomous")
        original = state.engine
        await state.reopen()
        assert state.engine is not original
        assert await state.tasks.get_task("public") is not None

    async def interrupted() -> None:
        async with local_workflow(tmp_path):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await interrupted()


async def test_e2e_runner_reports_completion_separately_and_excludes_raw_text(
    tmp_path: Path,
) -> None:
    suite, digest = load_suite(PUBLIC / "public_v1.json")
    directory = await run_evaluation(
        suite,
        dataset_sha256=digest,
        adapters=[adapter(tmp_path / "state")],
        configuration=RunConfiguration(profile="injected", case_timeout_seconds=30.0),
        output_root=tmp_path / "reports",
        repo_root=ROOT,
    )
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    group = summary["groups"][0]
    assert group["check_pass_rate"] == 1.0
    assert group["task_success_rate"] == 0.6
    assert group["usage"]["external_api_calls"] == group["usage"]["total_tokens"] == 0
    assert group["usage"]["budget_tokens"] > 0
    assert group["usage"]["monetary_cost"] is None
    assert group["metrics"]["checkpoint_correctness"]["scored"] == 4
    records = (directory / "cases.jsonl").read_text(encoding="utf-8")
    report = (directory / "evaluation_report.md").read_text(encoding="utf-8")
    fixture, _ = load_fixture(PUBLIC / "workflow_fixture_v1.json", WorkflowFixture)
    for case in suite.cases:
        assert case.prompt not in records + report
    for source in fixture.sources.values():
        assert source.content not in records + report


def test_case_bindings_and_labels_are_validated_before_execution(tmp_path: Path) -> None:
    suite, _ = load_suite(PUBLIC / "public_v1.json")
    injected = adapter(tmp_path)
    case = suite.cases[0].model_copy(update={"prompt": "changed public goal"})
    with pytest.raises(ValueError):
        injected.validate_case(case)
    invalid = dict(suite.cases[3].expected, task_success=True)
    with pytest.raises(ValueError):
        WorkflowLabels.model_validate(invalid)
    assert WorkflowObservation.model_validate(completed_payload()).task_completed


class HeldWorkflowSandbox(WorkflowReplaySandbox):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.interrupted = False

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.requests.append(request)
        self.entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("Held fake execution unexpectedly resumed")
        finally:
            self.interrupted = True


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_e2e_runner_joins_active_workflow_on_timeout_or_cancel(
    tmp_path: Path,
    stop: str,
) -> None:
    suite, digest = load_suite(PUBLIC / "public_v1.json")
    suite = suite.model_copy(update={"cases": suite.cases[:1]})
    sandbox = HeldWorkflowSandbox()
    evaluation = asyncio.create_task(
        run_evaluation(
            suite,
            dataset_sha256=digest,
            adapters=[adapter(tmp_path / "state", sandbox)],
            configuration=RunConfiguration(profile="injected", case_timeout_seconds=4.0),
            output_root=tmp_path / "reports",
            repo_root=ROOT,
        )
    )
    await asyncio.wait_for(sandbox.entered.wait(), timeout=3)
    if stop == "cancel":
        evaluation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(evaluation, timeout=10)
    else:
        await asyncio.wait_for(evaluation, timeout=10)
    assert sandbox.interrupted
    assert current_budget.get() is None and current_journal.get() is None
    directory = next((tmp_path / "reports").glob("eval-*"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    group = summary["groups"][0]
    assert group["recorded"] == group["planned"] == 1
    assert group["status_counts"] == {"timeout" if stop == "timeout" else "cancelled": 1}
    assert group["check_pass_rate"] == group["task_success_rate"] == 0
    assert group["usage"]["model_calls"] is None  # Interrupted case did not return measured usage.
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == ("completed" if stop == "timeout" else "cancelled")
