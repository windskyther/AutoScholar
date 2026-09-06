from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

TaskStatus = Literal["running", "succeeded", "failed", "budget_exceeded"]
ToolCallStatus = Literal["succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class ToolTraceRecord:
    id: str
    task_id: str
    sequence: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    output: str
    status: ToolCallStatus
    duration_ms: float
    created_at: datetime
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTaskRecord:
    id: str
    status: TaskStatus
    objective: str
    plan: list[str]
    answer: str | None
    metrics: dict[str, int]
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None
    tool_calls: list[ToolTraceRecord] = field(default_factory=list)
