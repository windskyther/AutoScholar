from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from autoscholar.experiment.models import ExperimentSpecification


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    type: Literal["research", "knowledge", "coding", "experiment"]
    description: str = Field(min_length=1, max_length=2000)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    expected_output: str = Field(min_length=1, max_length=1000)
    specification: ExperimentSpecification | None = None


class TaskPlan(StrictModel):
    goal: str = Field(min_length=1, max_length=10000)
    steps: list[PlanStep] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_graph(self) -> "TaskPlan":
        by_id = {step.id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("step identifiers must be unique")
        for step in self.steps:
            if len(set(step.dependencies)) != len(step.dependencies):
                raise ValueError("duplicate dependency")
            if step.id in step.dependencies or not set(step.dependencies) <= by_id.keys():
                raise ValueError("unknown or self dependency")
            if step.specification is not None and step.type != "experiment":
                raise ValueError("only experiments accept a specification")
        pending = set(by_id)
        done: set[str] = set()
        while pending:
            ready = {key for key in pending if set(by_id[key].dependencies) <= done}
            if not ready:
                raise ValueError("plan contains a dependency cycle")
            done |= ready
            pending -= ready
        for step in self.steps:
            if step.type == "experiment":
                coding = [key for key in step.dependencies if by_id[key].type == "coding"]
                if len(coding) != 1:
                    raise ValueError("each experiment needs exactly one direct coding dependency")
        return self


class ReviewIssue(StrictModel):
    code: str = Field(min_length=1, max_length=100)
    step_id: str = Field(min_length=1, max_length=40)
    message: str = Field(min_length=1, max_length=2000)


class ReviewResult(StrictModel):
    status: Literal["PASS", "REPLAN"]
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=30)
    suggested_steps: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_verdict(self) -> "ReviewResult":
        if (self.status == "PASS" and self.issues) or (self.status == "REPLAN" and not self.issues):
            raise ValueError("review verdict and issues disagree")
        return self


class PlanRevision(StrictModel):
    plan: TaskPlan
    rerun_steps: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_reruns(self) -> "PlanRevision":
        if not set(self.rerun_steps) <= {step.id for step in self.plan.steps}:
            raise ValueError("rerun references unknown step")
        return self
