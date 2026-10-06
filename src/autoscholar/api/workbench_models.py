"""Bounded browser-facing records; no runtime credentials or checkpoint internals."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from autoscholar.agent.records import ResolvedAgentMode, TaskStatus
from autoscholar.api.routes.agent import AgentMetricsResponse, EvidenceResponse
from autoscholar.api.routes.experiments import ArtifactResponse, ExperimentResponse
from autoscholar.core.budget import BudgetLimits
from autoscholar.orchestration.models import TaskPlan


class TaskSummary(BaseModel):
    task_id: str
    project_id: str | None
    parent_task_id: str | None
    objective: str
    mode: ResolvedAgentMode
    status: TaskStatus
    metrics: AgentMetricsResponse
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    project_id: str
    items: list[TaskSummary]
    total: int
    limit: int
    offset: int


class ChildTaskPage(BaseModel):
    items: list[TaskSummary]
    total: int
    limit: int
    offset: int


class PlanSummary(BaseModel):
    version: int
    plan: TaskPlan


class ExecutionSummary(BaseModel):
    status: TaskStatus
    stage: str | None
    plan_version: int | None
    checkpoint_sequence: int
    budget_used: dict[str, int]
    budget_limits: BudgetLimits | None
    active_seconds: float
    pending_call_count: int
    error_code: str | None


class ResourceCounts(BaseModel):
    evidence: int
    experiments: int
    artifacts: int


class TaskOverviewResponse(BaseModel):
    task: TaskSummary
    children: ChildTaskPage
    current_plan: PlanSummary | None
    execution: ExecutionSummary | None
    resources: ResourceCounts
    answer: str | None
    answer_truncated: bool
    # A single shared budget belongs to the root; never sum parent and child usage.
    usage_scope: str = "root_task_only"
    monetary_cost: None = None


class OwnedEvidenceResponse(EvidenceResponse):
    task_id: str


class FamilyEvidenceResponse(BaseModel):
    task_id: str
    items: list[OwnedEvidenceResponse]
    total: int
    limit: int
    offset: int


class FamilyExperimentResponse(BaseModel):
    task_id: str
    items: list[ExperimentResponse]
    total: int
    limit: int
    offset: int


class FamilyArtifactResponse(BaseModel):
    task_id: str
    items: list[ArtifactResponse]
    total: int
    limit: int
    offset: int


class WorkbenchSessionResponse(BaseModel):
    status: str = "connected"
    authentication: str = "single_operator_bearer"
    api_version: str
    capabilities: dict[str, Any]


class WorkflowEventSummary(BaseModel):
    task_id: str
    sequence: int
    kind: str
    payload: dict[str, str | int | bool]
    created_at: datetime


class WorkflowEventPage(BaseModel):
    task_id: str
    status: TaskStatus
    durable: bool
    items: list[WorkflowEventSummary]
    next_cursor: int
    has_more: bool
    has_older: bool
