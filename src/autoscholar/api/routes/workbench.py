"""Authenticated browser facade. Legacy REST paths remain explicitly unchanged."""

from fastapi import APIRouter, Depends, Query, Request

from autoscholar import __version__
from autoscholar.agent.records import ResolvedAgentMode, TaskStatus
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.experiment_auth import require_experiment_token
from autoscholar.api.routes.agent import router as agent_router
from autoscholar.api.routes.experiments import router as experiments_router
from autoscholar.api.routes.health import router as health_router
from autoscholar.api.routes.memory import router as memory_router
from autoscholar.api.routes.projects import router as projects_router
from autoscholar.api.routes.workflows import router as workflows_router
from autoscholar.api.workbench_models import (
    FamilyArtifactResponse,
    FamilyEvidenceResponse,
    FamilyExperimentResponse,
    TaskListResponse,
    TaskOverviewResponse,
    WorkbenchSessionResponse,
)
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.errors import AppError

router = APIRouter(
    prefix="/workbench", tags=["workbench"], dependencies=[Depends(require_experiment_token)]
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
            "llm_configured": settings.llm_configured,
            "web_search_configured": settings.web_search_configured,
            "research_backend": settings.research_tool_backend,
            "filesystem_backend": settings.filesystem_tool_backend,
            "experiment_backend": settings.experiment_tool_backend,
            "task_streaming": False,
            "document_max_bytes": settings.document_max_bytes,
            "document_max_pages": settings.document_max_pages,
            "budget_limits": settings.autonomous_budget.model_dump(),
        },
    )


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


for existing in (
    projects_router,
    agent_router,
    experiments_router,
    workflows_router,
    memory_router,
    health_router,
):
    router.include_router(existing)
