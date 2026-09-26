"""Versioned JSON checkpoints. No pickle, secrets, clients or executable objects."""

import hashlib
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from autoscholar.agent.records import ResearchSource
from autoscholar.core.budget import Budget, BudgetLimits
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.orchestration.models import ReviewResult, TaskPlan
from autoscholar.orchestration.service import Run
from autoscholar.rag.models import RetrievalMode

Stage = Literal["planner", "executor", "reviewer", "replanner", "writer", "done"]


def default_sources() -> list[ResearchSource]:
    return ["web", "paper"]


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    stage: Stage = "planner"
    task_id: str
    objective: str = Field(min_length=1, max_length=10000)
    specification: ExperimentSpecification = Field(default_factory=ExperimentSpecification)
    limits: BudgetLimits = Field(default_factory=BudgetLimits)
    project_id: str | None = None
    document_ids: list[str] | None = None
    retrieval_mode: RetrievalMode = "hybrid_rerank"
    research_sources: list[ResearchSource] = Field(default_factory=default_sources)
    plan: TaskPlan | None = None
    version: int = Field(default=1, ge=1)
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sources: dict[str, dict[str, str]] = Field(default_factory=dict)
    previous_sources: dict[str, dict[str, str]] = Field(default_factory=dict)
    review: ReviewResult | None = None
    answer: str | None = None
    traces: int = 0
    approval_threshold: int = Field(default=20000, ge=0)
    memory_context: dict[str, Any] = Field(default_factory=dict)

    def restore(self, usage: dict[str, int], active_seconds: float) -> Run:
        names = (
            "task_id",
            "objective",
            "specification",
            "project_id",
            "document_ids",
            "retrieval_mode",
            "research_sources",
            "plan",
            "version",
            "results",
            "sources",
            "previous_sources",
            "review",
            "answer",
            "traces",
            "memory_context",
        )
        return Run(
            **{name: getattr(self, name) for name in names},
            budget=Budget(self.limits, dict(usage), time.monotonic() - active_seconds),
        )

    def capture(self, run: Run, stage: Stage) -> "Snapshot":
        values = self.model_dump()
        for name in (
            "plan",
            "version",
            "results",
            "sources",
            "previous_sources",
            "review",
            "answer",
            "traces",
            "specification",
            "memory_context",
        ):
            values[name] = getattr(run, name)
        values["stage"] = stage
        return Snapshot.model_validate(values)
