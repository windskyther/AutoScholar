from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from autoscholar.api.routes.workflows import service
from autoscholar.orchestration.memory import ProjectMemoryUpdate

router = APIRouter(prefix="/projects", tags=["memory"])


class MemoryEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("/{project_id}/memory")
async def project_memory(project_id: str, request: Request) -> dict[str, Any]:
    return await service(request).memory.project(project_id)


@router.put("/{project_id}/memory")
async def update_project_memory(
    project_id: str,
    payload: ProjectMemoryUpdate,
    request: Request,
) -> dict[str, Any]:
    return await service(request).memory.update(project_id, payload)


@router.get("/{project_id}/experiences")
async def experiences(
    project_id: str,
    request: Request,
    problem_code: str | None = Query(default=None, max_length=100),
    include_disabled: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "items": await service(request).memory.experiences(
            project_id,
            problem_code=problem_code,
            include_disabled=include_disabled,
            limit=limit,
        ),
    }


@router.patch("/{project_id}/experiences/{memory_id}")
async def enable_experience(
    project_id: str,
    memory_id: str,
    payload: MemoryEnabled,
    request: Request,
) -> dict[str, Any]:
    await service(request).memory.set_enabled(project_id, memory_id, payload.enabled)
    return {"id": memory_id, "enabled": payload.enabled}
