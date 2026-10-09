"""Real durable workflow evaluation with scripted decisions and independent saved-output grading."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from autoscholar.agent.records import TaskStatus
from autoscholar.coding.sandbox import SandboxExecutor
from autoscholar.evaluation.component_fixture import QueryInputs, fixture_usage
from autoscholar.evaluation.experiment_adapter import (
    ExperimentVerification,
    verify_completed_experiment,
)
from autoscholar.evaluation.isolated_components import ObservedSandbox
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    Observation,
    ScoreCard,
)
from autoscholar.evaluation.research_adapter import TOPICS
from autoscholar.evaluation.workflow_fixture import (
    ScriptedWorkflowProvider,
    WorkflowFaultSandbox,
    WorkflowFixture,
    WorkflowSearch,
    workflow_plan,
)
from autoscholar.evaluation.workflow_runtime import local_workflow, workflow_service
from autoscholar.orchestration.service import source_digest


class StepCounts(EvaluationModel):
    research: int = Field(ge=0, le=2)
    code: int = Field(ge=0, le=2)
    train: int = Field(ge=0, le=2)


class WorkflowLabels(EvaluationModel):
    workflow_status: Literal["succeeded", "budget_exceeded"]
    error_code: Literal["autonomous_budget_exceeded"] | None = None
    task_success: bool
    verification: Literal["accepted", "rejected", "not_run"]
    step_runs: StepCounts
    replans: int = Field(ge=0, le=1)
    training_runs: int = Field(ge=0, le=2)
    restarts: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def coherent(self) -> "WorkflowLabels":
        if (self.workflow_status == "budget_exceeded") != bool(self.error_code):
            raise ValueError("Budget refusal needs its exact error code")
        if self.task_success != (
            self.workflow_status == "succeeded" and self.verification == "accepted"
        ):
            raise ValueError("Only a completed independently verified task can succeed")
        if self.workflow_status == "budget_exceeded" and self.verification != "not_run":
            raise ValueError("This early budget case cannot claim independent grading")
        return self


class WorkflowObservation(EvaluationModel):
    workflow_status: TaskStatus
    error_code: str | None = Field(default=None, max_length=100)
    step_runs: StepCounts
    replans: int = Field(ge=0, le=1)
    training_runs: int = Field(ge=0, le=2)
    restarts: int = Field(ge=0, le=1)
    checkpoints: int = Field(ge=0, le=50)
    trace_bound: bool
    steps_complete: bool
    usage_consistent: bool
    resume_verified: bool | None = None
    code_checks_passed: bool | None = None
    evidence_bound: bool | None = None
    source_handoff: bool | None = None
    report_published: bool
    report_bound: bool | None = None
    latest_results_only: bool | None = None
    verification: ExperimentVerification | None = None
    execution_sandbox_runs: int = Field(ge=0, le=16)
    grading_sandbox_runs: int = Field(ge=0, le=1)

    @property
    def task_completed(self) -> bool:
        return (
            self.workflow_status == "succeeded"
            and self.trace_bound
            and self.steps_complete
            and self.usage_consistent
            and self.code_checks_passed is True
            and self.evidence_bound is True
            and self.source_handoff is True
            and self.report_published
            and self.report_bound is True
            and self.latest_results_only is True
            and self.grading_sandbox_runs == 1
            and (self.restarts == 0 or self.resume_verified is True)
            and self.verification is not None
            and self.verification.accepted
        )


def score_workflow(observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
    actual = WorkflowObservation.model_validate(observation.payload)
    labels = WorkflowLabels.model_validate(expected)
    verified = actual.verification
    oracle = verified.oracle if verified else None
    rejected = oracle is not None and verified is not None and not verified.accepted
    checks = {
        "workflow_status": actual.workflow_status == labels.workflow_status,
        "error_classification": actual.error_code == labels.error_code,
        "task_outcome": actual.task_completed == labels.task_success,
        "step_runs": actual.step_runs == labels.step_runs,
        "replan_count": actual.replans == labels.replans,
        "training_count": actual.training_runs == labels.training_runs,
        "restart_count": actual.restarts == labels.restarts,
        "persisted_checkpoints": actual.checkpoints > 0,
        "trace_scope": actual.trace_bound,
        "persisted_usage": actual.usage_consistent,
        "independent_verification": (
            verified is not None and verified.accepted
            if labels.verification == "accepted"
            else rejected
            if labels.verification == "rejected"
            else verified is None and actual.grading_sandbox_runs == 0
        ),
    }
    if labels.restarts:
        checks["resume_without_replaying_code"] = actual.resume_verified is True
    if labels.workflow_status == "succeeded":
        checks.update(
            {
                "complete_plan": actual.steps_complete,
                "mandatory_code_validation": actual.code_checks_passed is True,
                "bound_evidence_and_citations": actual.evidence_bound is True,
                "source_handoff": actual.source_handoff is True,
                "report_matches_saved_results": actual.report_bound is True,
                "latest_reviewed_results_only": actual.latest_results_only is True,
            }
        )
    else:
        checks["no_failed_report_published"] = not actual.report_published
    checkpoint_correctness = (
        None if oracle is None else float(all(item.verified for item in oracle.models))
    )
    return ScoreCard(
        checks=checks,
        task_success=actual.task_completed,
        metrics={
            "workflow_completion": float(actual.workflow_status == "succeeded"),
            "task_completion": float(actual.task_completed),
            "expected_noncompletion": float(not labels.task_success),
            "replans": float(actual.replans),
            "training_runs": float(actual.training_runs),
            "restarts": float(actual.restarts),
            "checkpoints": float(actual.checkpoints),
            "code_validation": None
            if actual.code_checks_passed is None
            else float(actual.code_checks_passed),
            "evidence_binding": None
            if actual.evidence_bound is None
            else float(actual.evidence_bound),
            "source_handoff": None
            if actual.source_handoff is None
            else float(actual.source_handoff),
            "report_consistency": None
            if actual.report_bound is None
            else float(actual.report_bound),
            "checkpoint_correctness": checkpoint_correctness,
            "execution_sandbox_runs": float(actual.execution_sandbox_runs),
            "grading_sandbox_runs": float(actual.grading_sandbox_runs),
        },
    )


class InjectedWorkflowAdapter:
    def __init__(
        self,
        fixture: WorkflowFixture,
        *,
        fixture_sha256: str,
        sandbox: SandboxExecutor,
        resources: dict[str, str],
        workspace_root: Path,
    ) -> None:
        self.fixture = fixture
        self.sandbox = sandbox
        self.resources = resources
        self.workspace_root = workspace_root
        templates = Path(__file__).resolve().parents[1] / "experiment"
        self.train_source = (templates / "train_template.py").read_text(encoding="utf-8")
        self.test_source = (templates / "test_template.py").read_text(encoding="utf-8")
        self.oracle_source = (
            Path(__file__).with_name("experiment_oracle.py").read_text(encoding="utf-8")
        )
        self.identity = AdapterIdentity(
            name="isolated-durable-workflow",
            category="end_to_end",
            variant="baseline",
            execution="injected",
            model="scripted-public-workflow",
            resources={
                **resources,
                "workflow_fixture": fixture_sha256,
                "train_template": hashlib.sha256(self.train_source.encode()).hexdigest(),
                "test_template": hashlib.sha256(self.test_source.encode()).hexdigest(),
                "checkpoint_oracle": hashlib.sha256(self.oracle_source.encode()).hexdigest(),
            },
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = QueryInputs.model_validate(case.inputs)
        WorkflowLabels.model_validate(case.expected)
        script = self.fixture.scripts.get(query.query_id)
        if script is None or script.prompt != case.prompt:
            raise ValueError("Workflow fixture input binding differs")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = QueryInputs.model_validate(inputs)
        script = self.fixture.scripts[query.query_id]
        if script.prompt != prompt:
            raise ValueError("Workflow query changed after preflight")
        provider = ScriptedWorkflowProvider(self.fixture, script)
        search = WorkflowSearch(self.fixture)
        recorded = ObservedSandbox(self.sandbox, self.resources)
        faulted = WorkflowFaultSandbox(recorded, script.fault)
        restarted = 0
        restart_bound: bool | None = None
        saved_code_child: str | None = None
        saved_usage: dict[str, int] = {}
        async with local_workflow(self.workspace_root) as state:
            durable = workflow_service(state, provider, search, faulted, script)
            task_id, _ = await durable.submit(
                {
                    "objective": prompt,
                    "research_sources": ["web"],
                    "experiment_specification": script.specification.model_dump(),
                    "budget": script.limits.model_dump(),
                },
                "public-case",
            )
            for _ in range(20):
                if not await durable.tick(task_id):
                    raise ValueError("Controlled workflow stopped progressing")
                snapshot, job = await durable.repository.snapshot(task_id)
                if (
                    script.restart_after_coding
                    and not restarted
                    and snapshot.results.get("code", {}).get("status") == "succeeded"
                ):
                    saved_code_child = snapshot.results["code"]["child_task_id"]
                    saved_usage = dict(job.usage)
                    before = snapshot.model_dump()
                    old_owner = durable.owner
                    old_engine = state.engine
                    await state.reopen()
                    durable = workflow_service(state, provider, search, faulted, script)
                    restored, restored_job = await durable.repository.snapshot(task_id)
                    restarted = 1
                    restart_bound = (
                        state.engine is not old_engine
                        and durable.owner != old_owner
                        and restored.model_dump() == before
                        and restored_job.usage == saved_usage
                    )
                if job.status != "queued":
                    break
            else:
                raise ValueError("Controlled workflow exceeded its unit bound")
            snapshot, job = await durable.repository.snapshot(task_id)
            parent = await state.tasks.get_task(task_id)
            if parent is None:
                raise ValueError("Workflow task did not persist")
            steps = await durable.service.workflows.history(task_id, "steps")
            plans = await durable.service.workflows.history(task_id, "plans")
            checkpoints = await durable.repository.history(task_id, "checkpoints")
            counts = {
                key: sum(step["step_id"] == key for step in steps)
                for key in ("research", "code", "train")
            }
            bound = parent.status == job.status and all(step["step_id"] in counts for step in steps)
            for step in steps:
                child = await state.tasks.get_task(step["child_task_id"])
                bound = (
                    bound
                    and child is not None
                    and child.parent_task_id == task_id
                    and child.status == step["status"] == step["result"].get("status")
                    and step["result"].get("child_task_id") == step["child_task_id"]
                )
            complete = (
                snapshot.plan == workflow_plan(script)
                and set(snapshot.results) == {"research", "code", "train"}
                and all(result["status"] == "succeeded" for result in snapshot.results.values())
                and all(
                    any(
                        step["step_id"] == key
                        and step["result"] == result
                        and step["status"] == "succeeded"
                        for step in steps
                    )
                    for key, result in snapshot.results.items()
                )
                and snapshot.review is not None
                and snapshot.review.status == "PASS"
            )
            execution_count = len(recorded.runs)
            training_count = sum(
                request.action == "run_python" and request.path == "train.py"
                for request, _ in recorded.runs
            )
            usage_bound = (
                parent.metrics.get("model_calls", 0)
                == job.usage.get("model_calls", 0)
                == provider.calls
                and job.usage.get("input_tokens", 0) == 2 * provider.calls
                and job.usage.get("output_tokens", 0) == provider.calls
                and job.usage.get("total_tokens", 0) == 3 * provider.calls
                and job.usage.get("sandbox_runs", 0) == execution_count
                and job.usage.get("training_runs", 0) == training_count
                and job.usage.get("search_queries", 0) == search.calls
                and job.usage.get("steps", 0) == len(steps)
                and job.usage.get("replans", 0) == max(0, len(plans) - 1)
            )
            if restarted:
                restart_bound = (
                    bool(restart_bound)
                    and counts["code"] == 1
                    and snapshot.results.get("code", {}).get("child_task_id") == saved_code_child
                    and all(job.usage.get(key, 0) >= value for key, value in saved_usage.items())
                )
            evidence_bound: bool | None = None
            code_checks: bool | None = None
            handoff: bool | None = None
            report_bound: bool | None = None
            latest_only: bool | None = None
            verification: ExperimentVerification | None = None
            research_result = snapshot.results.get("research", {})
            if research_result.get("status") == "succeeded":
                research = await state.tasks.get_task(research_result["child_task_id"])
                if research is None:
                    raise ValueError("Research child missing")
                expected_citations = {
                    (self.fixture.sources[topic].content, (f"E{index}",))
                    for index, topic in enumerate(TOPICS, 1)
                }
                evidence_bound = (
                    len(research.evidence) == 3
                    and len(research.citations) == 3
                    and {(item.claim, item.evidence_ids) for item in research.citations}
                    == expected_citations
                    and all(
                        item.external_id == topic
                        and item.citation_key == f"E{index}"
                        and item.claim == item.excerpt == self.fixture.sources[topic].content
                        and item.url == f"https://example.org/autoscholar-public/{topic}"
                        and f"{item.claim} [{item.citation_key}]" in (research.answer or "")
                        for index, (topic, item) in enumerate(
                            zip(TOPICS, research.evidence, strict=True), 1
                        )
                    )
                )
            code_result = snapshot.results.get("code", {})
            if code_result.get("status") == "succeeded":
                validated = [
                    (request, result)
                    for request, result in recorded.runs
                    if request.task_id == code_result["child_task_id"]
                    and request.action in ("static_check", "run_pytest")
                ]
                code_checks = {request.action for request, _ in validated} == {
                    "static_check",
                    "run_pytest",
                } and all(
                    result.status == "succeeded"
                    and result.exit_code == 0
                    and not result.truncated
                    and source_digest(request.files) == code_result["source_sha256"]
                    for request, result in validated
                )
            train_result = snapshot.results.get("train", {})
            if train_result.get("status") == "succeeded":
                code_source = state.workspace.source_snapshot(code_result["child_task_id"])
                transferred = {
                    **code_source,
                    "experiment_config.json": json.dumps(
                        script.specification.model_dump(), indent=2
                    ),
                }
                experiment_source = state.workspace.source_snapshot(train_result["child_task_id"])
                expected_sha = source_digest(transferred)
                handoff = (
                    source_digest(code_source) == code_result["source_sha256"]
                    and experiment_source == transferred
                    and train_result["source_sha256"]
                    == train_result["expected_source_sha256"]
                    == expected_sha
                )
                verification = await verify_completed_experiment(
                    task_id=train_result["child_task_id"],
                    store=state.tasks,
                    workspace=state.workspace,
                    sandbox=recorded,
                    resources=self.resources,
                    specification=script.specification,
                    train_source=self.train_source,
                    oracle_source=self.oracle_source,
                )
                child = await state.tasks.get_task(train_result["child_task_id"])
                answer = parent.answer or ""
                report_bound = (
                    parent.status == "succeeded"
                    and child is not None
                    and bool(child.answer)
                    and (child.answer or "") in answer
                    and f"Experiment: {train_result['experiment_id']}" in answer
                    and f"Source SHA-256: {expected_sha}" in answer
                    and f"Review: PASS; plan version: {snapshot.version}." in answer
                    and verification.metric_extraction_valid
                    and evidence_bound is True
                    and all(
                        f"{research_result['child_task_id']}:E{index}" in answer
                        for index in (1, 2, 3)
                    )
                )
                earlier = [
                    step
                    for step in steps
                    if step["step_id"] == "train"
                    and step["child_task_id"] != train_result["child_task_id"]
                ]
                latest_only = bool(answer) and all(
                    step["child_task_id"] not in answer for step in earlier
                )
                for previous in earlier:
                    previous_experiments = await state.tasks.list_experiments(
                        previous["child_task_id"]
                    )
                    latest_only = latest_only and all(
                        item.id not in answer for item in previous_experiments
                    )
            actual = WorkflowObservation.model_validate(
                {
                    "workflow_status": parent.status,
                    "error_code": parent.error_code
                    if parent.error_code in (None, "autonomous_budget_exceeded")
                    else "unexpected_workflow_error",
                    "step_runs": counts,
                    "replans": job.usage.get("replans", 0),
                    "training_runs": training_count,
                    "restarts": restarted,
                    "checkpoints": len(checkpoints),
                    "trace_bound": bound,
                    "steps_complete": complete,
                    "usage_consistent": usage_bound,
                    "resume_verified": restart_bound,
                    "code_checks_passed": code_checks,
                    "evidence_bound": evidence_bound,
                    "source_handoff": handoff,
                    "report_published": bool(parent.answer),
                    "report_bound": report_bound,
                    "latest_results_only": latest_only,
                    "verification": verification.model_dump(mode="json") if verification else None,
                    "execution_sandbox_runs": execution_count,
                    "grading_sandbox_runs": len(recorded.runs) - execution_count,
                }
            )
            usage = fixture_usage(model_calls=provider.calls).model_copy(
                update={"budget_tokens": job.usage.get("total_tokens", 0)}
            )
            return Observation(payload=actual.model_dump(mode="json"), usage=usage)

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_workflow(observation, expected)
