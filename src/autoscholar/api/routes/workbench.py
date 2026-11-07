"""Local browser facade for projects, managed models and research tasks."""

from typing import Annotated, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from autoscholar import __version__
from autoscholar.agent.records import ArtifactRecord, ResolvedAgentMode, TaskStatus
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.routes.agent import router as agent_router
from autoscholar.api.routes.health import router as health_router
from autoscholar.api.routes.memory import router as memory_router
from autoscholar.api.routes.projects import router as projects_router
from autoscholar.api.routes.workflows import router as workflows_router
from autoscholar.api.routes.workflows import service as workflow_service
from autoscholar.api.workbench_events import MAX_CURSOR, event_stream
from autoscholar.api.workbench_models import (
    FamilyArtifactResponse,
    FamilyEvidenceResponse,
    FamilyExperimentResponse,
    TaskListResponse,
    TaskOverviewResponse,
    WorkbenchApprovalPage,
    WorkbenchControlRequest,
    WorkbenchControlState,
    WorkbenchDecisionRequest,
    WorkbenchSessionResponse,
    WorkflowEventPage,
)
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.api.workbench_resources import (
    ArtifactPreview,
    ResourcePage,
    ResourceRepository,
    preview,
)
from autoscholar.core.errors import AppError
from autoscholar.experiment.artifacts import ArtifactError, ArtifactManager
from autoscholar.llm.profiles import ModelProfileCreate, ModelProfilePublic, ModelProfiles
from autoscholar.llm.usage import UsageSummary
from autoscholar.orchestration.approvals import ApprovalDecision
from autoscholar.orchestration.task_types import TASK_TYPES

router = APIRouter(
    prefix="/workbench", tags=["workbench"]
)


def store(request: Request) -> WorkbenchRepository:
    repository = request.app.state.agent_repository
    if not isinstance(repository, AgentTaskRepository):
        raise AppError(
            status_code=503,
            code="workbench_store_unavailable",
            message="Workbench storage is unavailable",
        )
    return WorkbenchRepository(repository.session_factory)


@router.get("/session", response_model=WorkbenchSessionResponse)
async def session(request: Request) -> WorkbenchSessionResponse:
    settings = request.app.state.settings
    return WorkbenchSessionResponse(
        api_version=__version__,
        capabilities={
            "llm_configured": any(item.enabled for item in await models(request).list()),
            "task_types": [
                {"id": key, "label": value["label"]} for key, value in TASK_TYPES.items()
            ],
            "model_management": True,
            "model_usage": True,
            "web_search_configured": settings.web_search_configured,
            "research_backend": settings.research_tool_backend,
            "filesystem_backend": settings.filesystem_tool_backend,
            "experiment_backend": settings.experiment_tool_backend,
            "task_streaming": True,
            "task_controls": True,
            "resource_browser": True,
            "project_deletion": True,
            "research_workflows": True,
            "task_deletion": True,
            "markdown_export": True,
            "document_max_bytes": settings.document_max_bytes,
            "document_max_pages": settings.document_max_pages,
            "budget_limits": settings.autonomous_budget.model_dump(),
        },
    )


def models(request: Request) -> ModelProfiles:
    profiles = request.app.state.model_profiles
    if not isinstance(profiles, ModelProfiles):
        raise AppError(
            status_code=503,
            code="workbench_store_unavailable",
            message="Model storage is unavailable",
        )
    return profiles


@router.get("/models")
async def list_models(request: Request) -> dict[str, object]:
    return {
        "items": await models(request).list(),
    }


@router.post("/models", response_model=ModelProfilePublic, status_code=201)
async def create_model(payload: ModelProfileCreate, request: Request) -> ModelProfilePublic:
    async with models(request).lock:
        return await models(request).create(payload)


@router.get("/usage", response_model=UsageSummary)
async def model_usage(
    request: Request,
    group_by: Literal["api", "model"] = "api",
    days: int = Query(default=30, ge=0, le=90),
    api_id: str | None = Query(default=None, min_length=1, max_length=64),
    model: str | None = Query(default=None, min_length=1, max_length=200),
) -> UsageSummary:
    if days not in {0, 7, 30, 90}:
        raise AppError(
            status_code=422, code="request_validation_error", message="Select 0, 7, 30 or 90 days"
        )
    return await models(request).usage.summary(
        group_by=group_by, days=cast(Literal[0, 7, 30, 90], days), api_id=api_id, model=model
    )


@router.post("/models/{profile_id}/{action}", response_model=ModelProfilePublic)
async def model_status(
    profile_id: str, action: Literal["enable", "disable"], request: Request
) -> ModelProfilePublic:
    return await models(request).enabled(profile_id, action == "enable")


@router.get("/tasks/{task_id}/controls", response_model=WorkbenchControlState)
async def controls(task_id: str, request: Request) -> WorkbenchControlState:
    return await store(request).controls(task_id)


@router.post("/tasks/{task_id}/control/{action}")
async def task_control(
    task_id: str,
    action: Literal["pause", "resume", "cancel"],
    payload: WorkbenchControlRequest,
    request: Request,
) -> dict[str, str]:
    await store(request).controls(task_id)
    status = await workflow_service(request).repository.control(
        task_id, action, expected=payload.expected.guard()
    )
    return {"task_id": task_id, "status": status}


@router.get("/tasks/{task_id}/approvals", response_model=WorkbenchApprovalPage)
async def task_approvals(
    task_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> WorkbenchApprovalPage:
    return await store(request).approvals(task_id, limit=limit, offset=offset)


@router.post("/tasks/{task_id}/approvals/{approval_id}/decision")
async def task_decision(
    task_id: str,
    approval_id: str,
    payload: WorkbenchDecisionRequest,
    request: Request,
) -> dict[str, str]:
    await store(request).controls(task_id)
    decision = ApprovalDecision.model_validate(payload.model_dump(exclude={"expected"}))
    status = await workflow_service(request).approvals.decide(
        task_id, approval_id, decision, expected=payload.expected.guard()
    )
    return {"task_id": task_id, "status": status}


@router.get("/projects/{project_id}/tasks", response_model=TaskListResponse)
async def tasks(
    project_id: str,
    request: Request,
    status: TaskStatus | None = None,
    mode: ResolvedAgentMode | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TaskListResponse:
    return await store(request).tasks(
        project_id, status=status, mode=mode, query=q, limit=limit, offset=offset
    )


@router.get("/tasks/{task_id}/overview", response_model=TaskOverviewResponse)
async def overview(
    task_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TaskOverviewResponse:
    return await store(request).overview(task_id, limit=limit, offset=offset)


@router.delete("/projects/{project_id}/tasks/{task_id}", status_code=204)
async def delete_task(project_id: str, task_id: str, request: Request) -> Response:
    await store(request).delete_task(project_id, task_id)
    return Response(status_code=204)


@router.get("/tasks/{task_id}/output/markdown")
async def export_markdown(task_id: str, request: Request) -> Response:
    objective, answer = await store(request).markdown(task_id)
    title = " ".join(objective.split())
    return Response(
        content=f"# {title}\n\n{answer}" + ("" if answer.endswith("\n") else "\n"),
        media_type="text/markdown",
        headers={
            "Content-Disposition": "attachment; filename=research-output.md; filename*=UTF-8''"
            + quote(title[:80].translate(str.maketrans(r'/\:*?"<>|', "_" * 9)) + ".md", safe=""),
        },
    )


@router.get("/tasks/{task_id}/events", response_model=WorkflowEventPage)
async def events(
    task_id: str,
    request: Request,
    after: int | None = Query(default=None, ge=0, le=MAX_CURSOR),
    before: int | None = Query(default=None, ge=1, le=MAX_CURSOR),
    limit: int = Query(default=50, ge=1, le=100),
) -> WorkflowEventPage:
    if after is not None and before is not None:
        raise AppError(
            status_code=422, code="event_cursor_invalid", message="Use only one event cursor"
        )
    return await store(request).events(task_id, after=after, before=before, limit=limit)


@router.get("/tasks/{task_id}/stream")
async def stream(
    task_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header(pattern=r"^[0-9]{1,16}$")] = None,
    after: int | None = Query(default=None, ge=0, le=MAX_CURSOR),
) -> StreamingResponse:
    header_cursor = int(last_event_id) if last_event_id is not None else None
    if header_cursor is not None and (
        header_cursor > MAX_CURSOR or (after is not None and after != header_cursor)
    ):
        raise AppError(status_code=422, code="event_cursor_invalid", message="Invalid event cursor")
    repository = store(request)
    # Validate auth/root/cursor/storage before sending a 200 streaming response.
    first = await repository.events(
        task_id, after=header_cursor if header_cursor is not None else (after or 0), limit=100
    )
    return StreamingResponse(
        event_stream(repository, request, first),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "private, no-store"},
    )


@router.get("/tasks/{task_id}/evidence", response_model=FamilyEvidenceResponse)
async def evidence(
    task_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> FamilyEvidenceResponse:
    result = await store(request).resources(task_id, "evidence", limit=limit, offset=offset)
    assert isinstance(result, FamilyEvidenceResponse)
    return result


@router.get("/tasks/{task_id}/experiments", response_model=FamilyExperimentResponse)
async def experiments(
    task_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> FamilyExperimentResponse:
    result = await store(request).resources(task_id, "experiments", limit=limit, offset=offset)
    assert isinstance(result, FamilyExperimentResponse)
    return result


@router.get("/tasks/{task_id}/artifacts", response_model=FamilyArtifactResponse)
async def artifacts(
    task_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> FamilyArtifactResponse:
    result = await store(request).resources(task_id, "artifacts", limit=limit, offset=offset)
    assert isinstance(result, FamilyArtifactResponse)
    return result


@router.get("/tasks/{task_id}/resources/{kind}", response_model=ResourcePage)
async def resource_page(
    task_id: str,
    kind: Literal["evidence", "experiments", "artifacts"],
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ResourcePage:
    return await ResourceRepository(store(request)).page(task_id, kind, limit=limit, offset=offset)


async def verified_content(
    task_id: str, artifact_id: str, request: Request
) -> tuple[ArtifactRecord, bytes]:
    record = await ResourceRepository(store(request)).artifact(task_id, artifact_id)
    manager = request.app.state.artifact_manager
    if not isinstance(manager, ArtifactManager):
        raise AppError(
            status_code=503,
            code="artifact_store_not_available",
            message="Artifact storage is unavailable",
        )
    try:
        content = await run_in_threadpool(manager.read_verified, record.task_id, record)
    except ArtifactError as exc:
        raise AppError(status_code=409, code=exc.code, message=exc.message) from exc
    return record, content


@router.get(
    "/tasks/{task_id}/resources/artifacts/{artifact_id}/preview", response_model=ArtifactPreview
)
async def artifact_preview(task_id: str, artifact_id: str, request: Request) -> ArtifactPreview:
    record, content = await verified_content(task_id, artifact_id, request)
    return preview(record, content, task_id)


@router.get("/tasks/{task_id}/resources/artifacts/{artifact_id}/content")
async def artifact_content(task_id: str, artifact_id: str, request: Request) -> Response:
    record, content = await verified_content(task_id, artifact_id, request)
    path, sha256 = record.path, record.sha256
    # Never trust media types or filenames supplied by generated metadata.
    media_type = ArtifactManager.allowed[path][1]
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{path.rsplit("/", 1)[-1]}"',
            "X-Content-SHA256": sha256,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


for existing in (
    projects_router,
    agent_router,
    workflows_router,
    memory_router,
    health_router,
):
    router.include_router(existing)
