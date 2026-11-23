"""Local project evidence browsing, explicit history import and reuse receipts."""

from typing import Any, Literal, cast

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.errors import AppError
from autoscholar.evidence.backfill import backfill
from autoscholar.evidence.database import EvidenceReuseRow, ProjectClaimRow
from autoscholar.evidence.library import EvidenceLibrary
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow

router = APIRouter(tags=["project-evidence"])


def library(request: Request) -> EvidenceLibrary:
    value = getattr(request.app.state, "evidence_library", None)
    if not isinstance(value, EvidenceLibrary):
        raise AppError(
            status_code=503,
            code="project_evidence_unavailable",
            message="Project evidence storage is unavailable",
        )
    return value


class HistoryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after: str | None = Field(default=None, max_length=36)
    limit: int = Field(default=50, ge=1, le=50)


class RecheckPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    web_days: int = Field(ge=1, le=365)
    paper_days: int = Field(ge=1, le=365)


@router.get("/projects/{project_id}/evidence")
async def evidence_page(
    project_id: str,
    request: Request,
    q: str = Query(default="", max_length=200),
    status: Literal["all", "ready", "pending", "withdrawn"] = "all",
    source_type: Literal["web", "paper", "document"] | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=1000000),
) -> dict[str, Any]:
    return await library(request).page(
        project_id, query=q, status=status, source_type=source_type, limit=limit, offset=offset
    )


@router.get("/projects/{project_id}/evidence/policy")
async def get_policy(project_id: str, request: Request) -> dict[str, int]:
    return await library(request).policy(project_id)


@router.put("/projects/{project_id}/evidence/policy")
async def set_policy(project_id: str, payload: RecheckPolicy, request: Request) -> dict[str, int]:
    return await library(request).policy(project_id, payload.model_dump())


@router.post("/projects/{project_id}/evidence/backfill")
async def import_history(
    project_id: str, payload: HistoryImport, request: Request
) -> dict[str, Any]:
    return await backfill(library(request), project_id, after=payload.after, limit=payload.limit)


@router.post("/projects/{project_id}/evidence/{claim_id}/{action}")
async def evidence_state(
    project_id: str, claim_id: str, action: Literal["enable", "disable"], request: Request
) -> dict[str, Any]:
    await library(request).set_enabled(project_id, claim_id, action == "enable")
    return {"id": claim_id, "enabled": action == "enable"}


@router.get("/projects/{project_id}/evidence/items/{claim_id}")
async def evidence_item(project_id: str, claim_id: str, request: Request) -> dict[str, Any]:
    result = await library(request).page(project_id, claim_id=claim_id, limit=1)
    if not result["items"]:
        raise AppError(
            status_code=404, code="project_evidence_not_found", message="Evidence was not found"
        )
    return cast(dict[str, Any], result["items"][0])


@router.get("/tasks/{task_id}/evidence-reuse")
async def reuse_receipts(task_id: str, request: Request) -> dict[str, Any]:
    store = library(request)
    async with store.sessions() as session:
        await WorkbenchRepository.root(session, task_id)
        job = await session.get(WorkflowJobRow, task_id)
        checkpoint = (
            await session.scalar(
                select(WorkflowCheckpointRow).where(
                    WorkflowCheckpointRow.task_id == task_id,
                    WorkflowCheckpointRow.sequence == job.checkpoint_sequence,
                )
            )
            if job
            else None
        )
        payload = checkpoint.payload if checkpoint else {}
        context = payload.get("evidence_context", {})
        rows = list(
            (
                await session.execute(
                    select(EvidenceReuseRow, ProjectClaimRow)
                    .join(ProjectClaimRow, ProjectClaimRow.id == EvidenceReuseRow.claim_id)
                    .where(EvidenceReuseRow.task_id == task_id)
                    .order_by(ProjectClaimRow.id)
                    .limit(12)
                )
            ).all()
        )
        return {
            "enabled": payload.get("reuse_project_evidence", False),
            "note": context.get("note"),
            "selected": len(context.get("claims", [])),
            "adopted": sum(receipt.status == "adopted" for receipt, _ in rows),
            "retrieval_calls": (job.usage.get("search_queries", 0) if job else 0),
            "items": [
                {
                    "claim": claim.text,
                    "source_objective": claim.topic,
                    "source_task_id": claim.task_id,
                    "status": receipt.status,
                    "reason": receipt.reason,
                }
                for receipt, claim in rows
            ],
        }
