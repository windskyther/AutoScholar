"""Real durable workflow evaluation with scripted decisions and independent saved-output grading."""

import hashlib
import json
from contextlib import AsyncExitStack
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
from autoscholar.evaluation.workflow_ablation import (
    WORKFLOW_VARIANTS,
    ObservedWorkflowMemory,
    WorkflowVariant,
    public_project_context,
)
from autoscholar.evaluation.workflow_fixture import (
    ScriptedWorkflowProvider,
    WorkflowFaultSandbox,
    WorkflowFixture,
    WorkflowSearch,
    workflow_plan,
)
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE, workflow_knowledge
from autoscholar.evaluation.workflow_runtime import local_workflow, workflow_service
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.memory import ProjectMemoryUpdate
from autoscholar.orchestration.service import source_digest
from autoscholar.rag.repository import KnowledgeRepository


class StepCounts(EvaluationModel):
    research: int = Field(ge=0, le=2)
    code: int = Field(ge=0, le=2)
    train: int = Field(ge=0, le=2)


class WorkflowLabels(EvaluationModel):
    workflow_status: Literal["succeeded", "budget_exceeded", "failed"]
    error_code: Literal["autonomous_budget_exceeded", "evaluation_replanning_disabled"] | None = (
        None
    )
    task_success: bool
    verification: Literal["accepted", "rejected", "not_run"]
    step_runs: StepCounts
    replans: int = Field(ge=0, le=1)
    training_runs: int = Field(ge=0, le=2)
    restarts: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def coherent(self) -> "WorkflowLabels":
        if (
            self.error_code
            != {
                "succeeded": None,
                "budget_exceeded": "autonomous_budget_exceeded",
                "failed": "evaluation_replanning_disabled",
            }[self.workflow_status]
        ):
            raise ValueError("Refusal needs its exact error code")
        if self.task_success != (
            self.workflow_status == "succeeded" and self.verification == "accepted"
        ):
            raise ValueError("Only a completed independently verified task can succeed")
        if self.workflow_status == "budget_exceeded" and self.verification != "not_run":
            raise ValueError("This early budget case cannot claim independent grading")
        return self


class WorkflowAblationLabels(EvaluationModel):
    baseline: WorkflowLabels
    overrides: dict[WorkflowVariant, WorkflowLabels] = Field(default_factory=dict, max_length=4)

    @model_validator(mode="after")
    def no_baseline_override(self) -> "WorkflowAblationLabels":
        if "baseline" in self.overrides:
            raise ValueError("Baseline cannot override itself")
        return self

    def for_variant(self, variant: WorkflowVariant) -> dict[str, JsonValue]:
        return self.overrides.get(variant, self.baseline).model_dump(mode="json")


class WorkflowPath(EvaluationModel):
    planner_calls: int = Field(ge=0, le=2)
    reviewer_calls: int = Field(ge=0, le=2)
    replanner_calls: int = Field(ge=0, le=1)
    memory_reads: int = Field(ge=0, le=2)
    memory_contexts: int = Field(ge=0, le=2)
    memory_payloads: int = Field(ge=0, le=2)
    memory_events: int = Field(ge=0, le=2)
    memory_learning_calls: int = Field(ge=0, le=1)
    saved_plans: int = Field(ge=0, le=2)
    saved_reviews: int = Field(ge=0, le=2)
    static_plan: bool
    rules_only_reviews: bool


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
    execution_path: WorkflowPath | None = None

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
        ablation: bool = False,
        variant: WorkflowVariant = "baseline",
    ) -> None:
        if variant not in WORKFLOW_VARIANTS or (variant != "baseline" and not ablation):
            raise ValueError("Workflow ablation must be explicitly enabled")
        self.ablation = ablation
        self.variant = variant
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
            variant=variant,
            execution="injected",
            model="scripted-public-workflow",
            resources={
                **resources,
                "workflow_fixture": fixture_sha256,
                "train_template": hashlib.sha256(self.train_source.encode()).hexdigest(),
                "test_template": hashlib.sha256(self.test_source.encode()).hexdigest(),
                "checkpoint_oracle": hashlib.sha256(self.oracle_source.encode()).hexdigest(),
                **(
                    {
                        "ablation_policy": hashlib.sha256(
                            Path(__file__).with_name("workflow_ablation.py").read_bytes()
                        ).hexdigest(),
                        "public_memory": hashlib.sha256(
                            public_project_context().model_dump_json().encode()
                        ).hexdigest(),
                        "project_knowledge": hashlib.sha256(PUBLIC_KNOWLEDGE.encode()).hexdigest(),
                        "knowledge_algorithm": hashlib.sha256(
                            Path(__file__).with_name("retrieval_ablation.py").read_bytes()
                        ).hexdigest(),
                    }
                    if ablation
                    else {}
                ),
            },
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = QueryInputs.model_validate(case.inputs)
        if self.ablation:
            WorkflowAblationLabels.model_validate(case.expected)
        else:
            WorkflowLabels.model_validate(case.expected)
        script = self.fixture.scripts.get(query.query_id)
        if script is None or script.prompt != case.prompt:
            raise ValueError("Workflow fixture input binding differs")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = QueryInputs.model_validate(inputs)
        script = self.fixture.scripts[query.query_id]
        if script.prompt != prompt:
            raise ValueError("Workflow query changed after preflight")
        provider = ScriptedWorkflowProvider(
            self.fixture, script, project_source=PUBLIC_KNOWLEDGE if self.ablation else None
        )
        search = WorkflowSearch(self.fixture)
        recorded = ObservedSandbox(self.sandbox, self.resources)
        faulted = WorkflowFaultSandbox(recorded, script.fault)
        restarted = 0
        restart_bound: bool | None = None
        saved_code_child: str | None = None
        saved_usage: dict[str, int] = {}
        async with local_workflow(self.workspace_root) as state, AsyncExitStack() as stack:
            knowledge = None

            def services() -> DurableService:
                return workflow_service(
                    state,
                    provider,
                    search,
                    faulted,
                    script,
                    variant=self.variant,
                    observe_memory=self.ablation,
                    knowledge=knowledge,
                )

            durable = services()
            memories = [durable.memory]
            project_id = None
            if self.ablation:
                project = await KnowledgeRepository(state.tasks.session_factory).create_project(
                    name="Public workflow ablation", description="Isolated evaluation only"
                )
                project_id = project.id
                await durable.memory.update(
                    project_id,
                    ProjectMemoryUpdate(expected_version=0, context=public_project_context()),
                )
                knowledge = await stack.enter_async_context(workflow_knowledge(state, project_id))
                durable = services()
                memories = [durable.memory]
            task_id, _ = await durable.submit(
                {
                    "objective": prompt,
                    "research_sources": ["web"],
                    "experiment_specification": script.specification.model_dump(),
                    "budget": script.limits.model_dump(),
                    "project_id": project_id,
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
                    durable = services()
                    memories.append(durable.memory)
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
            path = None
            if self.ablation:
                observed = [item for item in memories if isinstance(item, ObservedWorkflowMemory)]
                reviews = await durable.service.workflows.history(task_id, "reviews")
                events = await durable.repository.history(task_id, "events", limit=500)
                path = WorkflowPath(
                    planner_calls=provider.stage_calls.get("submit_task_plan", 0),
                    reviewer_calls=provider.stage_calls.get("submit_review", 0),
                    replanner_calls=provider.stage_calls.get("submit_plan_revision", 0),
                    memory_reads=sum(item.reads for item in observed),
                    memory_contexts=sum(item.nonempty for item in observed),
                    memory_payloads=provider.memory_payloads,
                    memory_events=sum(item["kind"] == "memory_retrieved" for item in events),
                    memory_learning_calls=sum(item.learns for item in observed),
                    saved_plans=len(plans),
                    saved_reviews=len(reviews),
                    static_plan=bool(plans) and plans[0]["reason"] == "evaluation_static_plan",
                    rules_only_reviews=self.variant == "no_reviewer"
                    and bool(reviews)
                    and "submit_review" not in provider.stage_calls,
                )
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
                if self.ablation:
                    expected_citations.add((PUBLIC_KNOWLEDGE, ("E4",)))
                evidence_bound = (
                    len(research.evidence) == (4 if self.ablation else 3)
                    and len(research.citations) == (4 if self.ablation else 3)
                    and {(item.claim, item.evidence_ids) for item in research.citations}
                    == expected_citations
                    and all(
                        item.external_id == topic
                        and item.citation_key == f"E{index}"
                        and item.claim == item.excerpt == self.fixture.sources[topic].content
                        and item.url == f"https://example.org/autoscholar-public/{topic}"
                        and f"{item.claim} [{item.citation_key}]" in (research.answer or "")
                        for index, (topic, item) in enumerate(
                            zip(TOPICS, research.evidence[:3], strict=True), 1
                        )
                    )
                )
                if self.ablation:
                    document = research.evidence[-1]
                    evidence_bound = evidence_bound and (
                        document.source_type == "document"
                        and document.claim == document.excerpt == PUBLIC_KNOWLEDGE
                        and document.citation_key == "E4"
                        and document.document_id is not None
                        and document.chunk_id is not None
                        and document.url
                        == f"/projects/{project_id}/documents/{document.document_id}/content#page=1"
                        and f"{PUBLIC_KNOWLEDGE} [E4]" in (research.answer or "")
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
                        for index in ((1, 2, 3, 4) if self.ablation else (1, 2, 3))
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
                    if parent.error_code
                    in (None, "autonomous_budget_exceeded", "evaluation_replanning_disabled")
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
                    "execution_path": path.model_dump(mode="json") if path else None,
                }
            )
            usage = fixture_usage(model_calls=provider.calls).model_copy(
                update={"budget_tokens": job.usage.get("total_tokens", 0)}
            )
            return Observation(payload=actual.model_dump(mode="json"), usage=usage)

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        if not self.ablation:
            return score_workflow(observation, expected)
        labels = WorkflowAblationLabels.model_validate(expected).for_variant(self.variant)
        score = score_workflow(observation, labels)
        actual = WorkflowObservation.model_validate(observation.payload)
        path = actual.execution_path
        if path is None:
            score.checks["observed_execution_path"] = False
            return score
        score.checks.update(
            {
                "planner_path": path.planner_calls == (0 if self.variant == "no_planner" else 1)
                and path.static_plan == (self.variant == "no_planner"),
                "reviewer_path": path.reviewer_calls
                == (0 if self.variant == "no_reviewer" else path.saved_reviews)
                and path.rules_only_reviews
                == (self.variant == "no_reviewer" and path.saved_reviews > 0),
                "replanning_path": path.replanner_calls == actual.replans
                and (self.variant != "no_replanning" or actual.replans == 0),
                "memory_read_path": path.memory_reads
                == path.memory_contexts
                == path.memory_events
                == (
                    0
                    if self.variant == "no_memory"
                    else 1
                    + path.replanner_calls
                    + int(actual.error_code == "evaluation_replanning_disabled")
                ),
                "memory_payload_path": path.memory_payloads
                == (
                    0 if self.variant == "no_memory" else path.planner_calls + path.replanner_calls
                ),
                "memory_learning_path": path.memory_learning_calls
                == (
                    int(actual.workflow_status == "succeeded") if self.variant != "no_memory" else 0
                ),
            }
        )
        for key in (
            "planner_calls",
            "reviewer_calls",
            "replanner_calls",
            "memory_reads",
            "memory_contexts",
            "memory_payloads",
            "memory_events",
            "memory_learning_calls",
        ):
            score.metrics[key] = float(getattr(path, key))
        return score
