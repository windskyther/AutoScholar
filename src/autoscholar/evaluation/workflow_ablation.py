"""Evaluation-only execution ablations; never expose these policies to application routes."""

from typing import Any, Literal

from autoscholar.orchestration.memory import MemoryService, ProjectContext
from autoscholar.orchestration.models import ReviewResult, TaskPlan
from autoscholar.orchestration.service import AutonomousService, FlowState, Run, WorkflowError

WorkflowVariant = Literal["baseline", "no_planner", "no_reviewer", "no_memory", "no_replanning"]
WORKFLOW_VARIANTS: tuple[WorkflowVariant, ...] = (
    "baseline",
    "no_planner",
    "no_reviewer",
    "no_memory",
    "no_replanning",
)


class AblationWorkflowError(WorkflowError):
    code = "evaluation_replanning_disabled"


class AblationAutonomousService(AutonomousService):
    variant: WorkflowVariant = "baseline"
    static_plan: TaskPlan

    async def _planner(self, state: FlowState) -> FlowState:
        if self.variant != "no_planner":
            return await super()._planner(state)
        run = state["run"]
        plan = self.static_plan.model_copy(deep=True)
        self._validate_plan(run, plan)
        run.plan = plan
        await self.workflows.save_plan(run.task_id, run.version, plan, "evaluation_static_plan")
        return state

    async def _reviewer(self, state: FlowState) -> FlowState:
        if self.variant != "no_reviewer":
            return await super()._reviewer(state)
        # Remove ONLY the model Reviewer. Never disable rules, Writer recheck or oracle.
        run = state["run"]
        issues = await self._rules(run)
        run.review = ReviewResult(status="REPLAN" if issues else "PASS", issues=issues)
        await self.workflows.save_review(run.task_id, run.version, run.review)
        return state

    async def _replanner(self, state: FlowState) -> FlowState:
        if self.variant == "no_replanning":
            # Fail explicitly before consuming a replan/model/training operation.
            raise AblationWorkflowError("Evaluation disabled replanning; failed work is retained")
        return await super()._replanner(state)


class ObservedWorkflowMemory(MemoryService):
    """Actual project-memory reads/learning, or an explicit evaluation-only bypass."""

    enabled: bool = True
    reads: int = 0
    nonempty: int = 0
    learns: int = 0

    async def context(self, run: Run) -> dict[str, Any]:
        if not self.enabled:
            return {}
        self.reads += 1
        context = await super().context(run)
        self.nonempty += bool(context)
        return context

    async def learn(self, run: Run) -> int:
        if not self.enabled:
            return 0
        self.learns += 1
        return await super().learn(run)


def public_project_context() -> ProjectContext:
    # Public engineering constraints, not historical experiences fabricated as verified.
    return ProjectContext(
        research_topic="Public CPU MNIST engineering comparison",
        experiment_rules=[
            "Keep the requested seed and subset unchanged.",
            "A small subset accuracy is not a full MNIST benchmark.",
            "Never automatically replay an unresolved operation.",
        ],
    )
