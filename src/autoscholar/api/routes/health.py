import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from autoscholar import __version__
from autoscholar.infrastructure.base import ManagedDependency

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["autoscholar"] = "autoscholar"
    version: str


class DependencyStatus(BaseModel):
    status: Literal["ok", "error", "not_configured"]


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, DependencyStatus]
    capabilities: dict[str, DependencyStatus]


async def _check_dependency(dependency: ManagedDependency) -> DependencyStatus:
    try:
        healthy = await dependency.ping()
    except Exception:
        return DependencyStatus(status="error")
    return DependencyStatus(status="ok" if healthy else "error")


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(version=__version__)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse}},
)
async def ready(request: Request) -> ReadyResponse | JSONResponse:
    database: ManagedDependency = request.app.state.database
    redis: ManagedDependency = request.app.state.redis
    database_status, redis_status = await asyncio.gather(
        _check_dependency(database),
        _check_dependency(redis),
    )
    dependencies = {
        "postgres": database_status,
        "redis": redis_status,
    }
    llm_status: Literal["ok", "not_configured"] = (
        "ok" if request.app.state.llm_provider.configured else "not_configured"
    )
    research_services = request.app.state.research_services
    research_capabilities = {
        f"{service.source_type}_search": DependencyStatus(
            status="ok" if service.configured else "not_configured"
        )
        for service in research_services
    }
    capabilities = {
        "llm": DependencyStatus(status=llm_status),
        **research_capabilities,
    }
    is_ready = all(dependency.status == "ok" for dependency in dependencies.values())
    response = ReadyResponse(
        status="ready" if is_ready else "not_ready",
        dependencies=dependencies,
        capabilities=capabilities,
    )
    if is_ready:
        return response
    return JSONResponse(status_code=503, content=response.model_dump())
