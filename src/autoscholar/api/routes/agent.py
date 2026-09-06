from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, StringConstraints

from autoscholar.agent.records import AgentTaskRecord, TaskStatus, ToolCallStatus
from autoscholar.agent.runner import AgentService, TaskStore
from autoscholar.core.errors import AppError

router = APIRouter(prefix="/agent", tags=["agent"])
ObjectiveText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


class AgentRunRequest(BaseModel):
    objective: ObjectiveText


class AgentMetricsResponse(BaseModel):
    iterations: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ToolCallResponse(BaseModel):
    sequence: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    output: str
    status: ToolCallStatus
    error_code: str | None
    duration_ms: float
    created_at: datetime


class AgentRunResponse(BaseModel):
    task_id: str
    status: TaskStatus
    plan: list[str]
    answer: str | None
    tool_calls: list[ToolCallResponse]
    metrics: AgentMetricsResponse
    request_id: str


class AgentTaskResponse(AgentRunResponse):
    objective: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


def _tool_calls(task: AgentTaskRecord) -> list[ToolCallResponse]:
    return [
        ToolCallResponse(
            sequence=trace.sequence,
            call_id=trace.call_id,
            tool_name=trace.tool_name,
            arguments=trace.arguments,
            output=trace.output,
            status=trace.status,
            error_code=trace.error_code,
            duration_ms=trace.duration_ms,
            created_at=trace.created_at,
        )
        for trace in task.tool_calls
    ]


def _metrics(task: AgentTaskRecord) -> AgentMetricsResponse:
    return AgentMetricsResponse(**task.metrics)


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(payload: AgentRunRequest, request: Request) -> AgentRunResponse:
    runner: AgentService | None = request.app.state.agent_runner
    if runner is None:
        raise AppError(
            status_code=503,
            code="agent_not_available",
            message="Agent execution is unavailable without database and LLM configuration",
        )
    result = await runner.run(payload.objective)
    task = result.task
    return AgentRunResponse(
        task_id=task.id,
        status=task.status,
        plan=task.plan,
        answer=task.answer,
        tool_calls=_tool_calls(task),
        metrics=_metrics(task),
        request_id=request.state.request_id,
    )


@router.get("/tasks/{task_id}", response_model=AgentTaskResponse)
async def get_agent_task(task_id: str, request: Request) -> AgentTaskResponse:
    repository: TaskStore | None = request.app.state.agent_repository
    if repository is None:
        raise AppError(
            status_code=503,
            code="agent_store_not_available",
            message="Agent task storage is unavailable",
        )
    task = await repository.get_task(task_id)
    if task is None:
        raise AppError(
            status_code=404,
            code="agent_task_not_found",
            message="Agent task was not found",
        )
    return AgentTaskResponse(
        task_id=task.id,
        status=task.status,
        objective=task.objective,
        plan=task.plan,
        answer=task.answer,
        tool_calls=_tool_calls(task),
        metrics=_metrics(task),
        error_code=task.error_code,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        request_id=request.state.request_id,
    )
