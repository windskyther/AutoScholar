"""Durable autonomous task submission and lifecycle controls."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request

from autoscholar.api.experiment_auth import require_experiment_token
from autoscholar.api.routes.agent import AgentRunRequest
from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable import DurableService

router = APIRouter(prefix="/agent/tasks", tags=["workflows"])


def service(request: Request) -> DurableService:
    require_experiment_token(request)
    result: DurableService | None = request.app.state.durable_service
    if result is None:
        raise AppError(status_code=503, code="workflow_unavailable", message="Workflow unavailable")
    return result


@router.post("", status_code=202)
async def submit(
    payload: AgentRunRequest,
    request: Request,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ],
) -> dict[str, Any]:
    durable = service(request)
    if payload.mode != "autonomous":
        raise AppError(
            status_code=422,
            code="autonomous_mode_required",
            message="Durable submission requires explicit autonomous mode",
        )
    task_id, created = await durable.submit(payload.model_dump(mode="json"), idempotency_key)
    _, job = await durable.repository.snapshot(task_id)
    return {
        "task_id": task_id,
        "status": job.status,
        "created": created,
        "status_url": f"/agent/tasks/{task_id}",
    }


@router.post("/{task_id}/{action}")
async def control(
    task_id: str,
    action: Literal["pause", "resume", "cancel"],
    request: Request,
) -> dict[str, str]:
    durable = service(request)
    return {"task_id": task_id, "status": await durable.repository.control(task_id, action)}


@router.get("/{task_id}/execution")
async def execution(task_id: str, request: Request) -> dict[str, Any]:
    snapshot, job = await service(request).repository.snapshot(task_id)
    return {
        "task_id": task_id,
        "status": job.status,
        "stage": snapshot.stage,
        "plan_version": snapshot.version,
        "checkpoint_sequence": job.checkpoint_sequence,
        "budget_used": job.usage,
        "active_seconds": job.active_seconds,
        "budget_limits": snapshot.limits.model_dump(),
        "active": job.active,
        "pending_calls": job.pending_calls,
        "error_code": job.error_code,
    }


@router.get("/{task_id}/durable/{kind}")
async def history(
    task_id: str,
    kind: Literal["checkpoints", "events"],
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = await service(request).repository.history(task_id, kind, limit)
    return {"task_id": task_id, "items": items}
