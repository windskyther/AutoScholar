from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, StringConstraints

from autoscholar.agent.records import (
    AgentMode,
    AgentTaskRecord,
    CitationRecord,
    EvidenceRecord,
    ResearchWarningRecord,
    ResolvedAgentMode,
    TaskStatus,
    ToolCallStatus,
)
from autoscholar.agent.runner import AgentService, TaskStore
from autoscholar.core.errors import AppError

router = APIRouter(prefix="/agent", tags=["agent"])
ObjectiveText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


class AgentRunRequest(BaseModel):
    objective: ObjectiveText
    mode: AgentMode = "auto"


class AgentMetricsResponse(BaseModel):
    iterations: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class EvidenceResponse(BaseModel):
    id: str
    citation_key: str
    source_type: str
    provider: str
    title: str
    url: str
    authors: list[str]
    year: int | None
    external_id: str | None
    query: str
    topic: str
    claim: str
    excerpt: str
    relevance: float
    created_at: datetime
    document_id: str | None
    chunk_id: str | None
    page: int | None
    section: str | None


class CitationResponse(BaseModel):
    claim: str
    evidence_ids: list[str]


class ResearchWarningResponse(BaseModel):
    code: str
    message: str
    provider: str | None


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
    mode: ResolvedAgentMode
    evidence: list[EvidenceResponse]
    citations: list[CitationResponse]
    warnings: list[ResearchWarningResponse]


class AgentTaskResponse(AgentRunResponse):
    objective: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class EvidenceListResponse(BaseModel):
    task_id: str
    items: list[EvidenceResponse]
    total: int
    limit: int
    offset: int


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


def _evidence(item: EvidenceRecord) -> EvidenceResponse:
    return EvidenceResponse(
        id=item.id,
        citation_key=item.citation_key,
        source_type=item.source_type,
        provider=item.provider,
        title=item.title,
        url=item.url,
        authors=list(item.authors),
        year=item.year,
        external_id=item.external_id,
        query=item.query,
        topic=item.topic,
        claim=item.claim,
        excerpt=item.excerpt,
        relevance=item.relevance,
        created_at=item.created_at,
        document_id=item.document_id,
        chunk_id=item.chunk_id,
        page=item.page,
        section=item.section,
    )


def _citations(items: list[CitationRecord]) -> list[CitationResponse]:
    return [
        CitationResponse(claim=item.claim, evidence_ids=list(item.evidence_ids)) for item in items
    ]


def _warnings(items: list[ResearchWarningRecord]) -> list[ResearchWarningResponse]:
    return [
        ResearchWarningResponse(code=item.code, message=item.message, provider=item.provider)
        for item in items
    ]


def _run_response(task: AgentTaskRecord, request_id: str) -> AgentRunResponse:
    return AgentRunResponse(
        task_id=task.id,
        status=task.status,
        plan=task.plan,
        answer=task.answer,
        tool_calls=_tool_calls(task),
        metrics=_metrics(task),
        request_id=request_id,
        mode=task.mode,
        evidence=[_evidence(item) for item in task.evidence],
        citations=_citations(task.citations),
        warnings=_warnings(task.warnings),
    )


@router.post("/run", response_model=AgentRunResponse)
async def run_agent(payload: AgentRunRequest, request: Request) -> AgentRunResponse:
    runner: AgentService | None = request.app.state.agent_runner
    if runner is None:
        raise AppError(
            status_code=503,
            code="agent_not_available",
            message="Agent execution is unavailable without database and LLM configuration",
        )
    result = await runner.run(payload.objective, mode=payload.mode)
    return _run_response(result.task, request.state.request_id)


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
        mode=task.mode,
        evidence=[_evidence(item) for item in task.evidence],
        citations=_citations(task.citations),
        warnings=_warnings(task.warnings),
    )


@router.get("/tasks/{task_id}/evidence", response_model=EvidenceListResponse)
async def get_agent_evidence(
    task_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EvidenceListResponse:
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
    items, total = await repository.list_evidence(task_id, limit=limit, offset=offset)
    return EvidenceListResponse(
        task_id=task_id,
        items=[_evidence(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
