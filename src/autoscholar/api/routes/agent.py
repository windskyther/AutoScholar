from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator

from autoscholar.agent.records import (
    AgentMode,
    AgentTaskRecord,
    CitationRecord,
    EvidenceRecord,
    ResearchSource,
    ResearchWarningRecord,
    ResolvedAgentMode,
    TaskStatus,
    ToolCallStatus,
)
from autoscholar.agent.runner import AgentService, TaskStore
from autoscholar.coding.workspace import WorkspaceError, WorkspaceFile, WorkspaceManager
from autoscholar.core.errors import AppError
from autoscholar.rag.models import RetrievalMode

router = APIRouter(prefix="/agent", tags=["agent"])
ObjectiveText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


def _default_research_sources() -> list[ResearchSource]:
    return ["web", "paper"]


class AgentRunRequest(BaseModel):
    objective: ObjectiveText
    mode: AgentMode = "auto"
    project_id: str | None = None
    document_ids: list[str] | None = None
    retrieval_mode: RetrievalMode = "hybrid_rerank"
    research_sources: list[ResearchSource] = Field(
        default_factory=_default_research_sources, min_length=1, max_length=2
    )

    @field_validator("research_sources")
    @classmethod
    def validate_research_sources(
        cls, sources: list[ResearchSource]
    ) -> list[ResearchSource]:
        if len(sources) != len(set(sources)):
            raise ValueError("research_sources must not contain duplicates")
        return sources


class AgentMetricsResponse(BaseModel):
    iterations: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    sandbox_runs: int = 0
    repair_attempts: int = 0
    files_written: int = 0


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
    project_id: str | None
    research_sources: list[ResearchSource]


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


class WorkspaceFileResponse(BaseModel):
    path: str
    size_bytes: int
    sha256: str
    updated_at: datetime


class WorkspaceManifestResponse(BaseModel):
    task_id: str
    files: list[WorkspaceFileResponse]
    total_bytes: int


class WorkspaceFileContentResponse(WorkspaceFileResponse):
    content: str


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


def _workspace_file(item: WorkspaceFile) -> WorkspaceFileResponse:
    return WorkspaceFileResponse(
        path=item.path,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        updated_at=item.updated_at,
    )


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
        project_id=task.project_id,
        research_sources=task.research_sources,
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
    result = await runner.run(
        payload.objective,
        mode=payload.mode,
        project_id=payload.project_id,
        document_ids=payload.document_ids,
        retrieval_mode=payload.retrieval_mode,
        research_sources=payload.research_sources,
    )
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
        project_id=task.project_id,
        research_sources=task.research_sources,
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


async def _coding_task(task_id: str, request: Request) -> AgentTaskRecord:
    repository: TaskStore | None = request.app.state.agent_repository
    if repository is None:
        raise AppError(
            status_code=503,
            code="agent_store_not_available",
            message="Agent task storage is unavailable",
        )
    task = await repository.get_task(task_id)
    if task is None or task.mode != "coding":
        raise AppError(
            status_code=404,
            code="coding_workspace_not_found",
            message="Coding task workspace was not found",
        )
    return task


def _workspace_error(exc: WorkspaceError) -> AppError:
    status = 404 if exc.code in {"workspace_not_found", "workspace_file_not_found"} else 400
    if exc.code == "workspace_file_not_text":
        status = 415
    return AppError(status_code=status, code=exc.code, message=exc.message)


@router.get(
    "/tasks/{task_id}/workspace",
    response_model=WorkspaceManifestResponse,
)
async def get_agent_workspace(task_id: str, request: Request) -> WorkspaceManifestResponse:
    await _coding_task(task_id, request)
    manager: WorkspaceManager = request.app.state.workspace_manager
    try:
        files = manager.list_files(task_id)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    items = [_workspace_file(item) for item in files]
    return WorkspaceManifestResponse(
        task_id=task_id,
        files=items,
        total_bytes=sum(item.size_bytes for item in items),
    )


@router.get(
    "/tasks/{task_id}/workspace/files/{file_path:path}",
    response_model=WorkspaceFileContentResponse,
)
async def get_agent_workspace_file(
    task_id: str, file_path: str, request: Request
) -> WorkspaceFileContentResponse:
    await _coding_task(task_id, request)
    manager: WorkspaceManager = request.app.state.workspace_manager
    normalized = file_path.replace("\\", "/")
    area, separator, relative = normalized.partition("/")
    if not separator or area not in manager.directories:
        raise AppError(
            status_code=400,
            code="workspace_path_invalid",
            message="Path must include a valid workspace area",
        )
    try:
        content = manager.read_text(task_id, relative, area=area)
        item = next(entry for entry in manager.list_files(task_id) if entry.path == normalized)
    except WorkspaceError as exc:
        raise _workspace_error(exc) from exc
    except StopIteration as exc:
        raise AppError(
            status_code=404,
            code="workspace_file_not_found",
            message="Workspace file was not found",
        ) from exc
    return WorkspaceFileContentResponse(
        content=content,
        path=item.path,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        updated_at=item.updated_at,
    )
