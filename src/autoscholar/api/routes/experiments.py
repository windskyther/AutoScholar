from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from autoscholar.agent.records import ArtifactRecord, ExperimentRecord
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.experiment_auth import require_experiment_token
from autoscholar.core.errors import AppError
from autoscholar.experiment.artifacts import ArtifactError, ArtifactManager

router = APIRouter(prefix="/agent/tasks/{task_id}", tags=["experiments"])


class ExperimentResponse(BaseModel):
    id: str
    task_id: str
    name: str
    status: str
    specification: dict[str, Any]
    metrics: dict[str, Any]
    source_sha256: str | None
    dataset_id: str | None
    dataset_sha256: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ArtifactResponse(BaseModel):
    id: str
    task_id: str
    experiment_id: str
    type: str
    path: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class ExperimentListResponse(BaseModel):
    task_id: str
    items: list[ExperimentResponse]


class ArtifactListResponse(BaseModel):
    task_id: str
    items: list[ArtifactResponse]


def _experiment_record(record: ExperimentRecord) -> ExperimentResponse:
    return ExperimentResponse(**asdict(record))


def _artifact_record(record: ArtifactRecord) -> ArtifactResponse:
    return ArtifactResponse(**asdict(record))


async def _repository(task_id: str, request: Request) -> AgentTaskRepository:
    require_experiment_token(request)
    repository = request.app.state.agent_repository
    if not isinstance(repository, AgentTaskRepository):
        raise AppError(
            status_code=503,
            code="experiment_store_not_available",
            message="Experiment storage is unavailable",
        )
    task = await repository.get_task(task_id)
    if task is None or task.mode != "experiment":
        raise AppError(
            status_code=404,
            code="experiment_task_not_found",
            message="Experiment task was not found",
        )
    return repository


@router.get("/experiments", response_model=ExperimentListResponse)
async def list_experiments(task_id: str, request: Request) -> ExperimentListResponse:
    repository = await _repository(task_id, request)
    records = await repository.list_experiments(task_id)
    return ExperimentListResponse(
        task_id=task_id, items=[_experiment_record(item) for item in records]
    )


@router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    task_id: str, experiment_id: str, request: Request
) -> ExperimentResponse:
    repository = await _repository(task_id, request)
    record = await repository.get_experiment(experiment_id)
    if record is None or record.task_id != task_id:
        raise AppError(
            status_code=404,
            code="experiment_not_found",
            message="Experiment was not found in this task",
        )
    return _experiment_record(record)


@router.get("/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(task_id: str, request: Request) -> ArtifactListResponse:
    repository = await _repository(task_id, request)
    records = await repository.list_artifacts(task_id)
    return ArtifactListResponse(
        task_id=task_id, items=[_artifact_record(item) for item in records]
    )


@router.get("/artifacts/{artifact_id}")
async def download_artifact(task_id: str, artifact_id: str, request: Request) -> Response:
    repository = await _repository(task_id, request)
    record = await repository.get_artifact(artifact_id)
    if record is None or record.task_id != task_id:
        raise AppError(
            status_code=404,
            code="artifact_not_found",
            message="Artifact was not found in this task",
        )
    manager: ArtifactManager | None = request.app.state.artifact_manager
    if manager is None:
        raise AppError(
            status_code=503,
            code="artifact_store_not_available",
            message="Artifact storage is unavailable",
        )
    try:
        content = manager.read_verified(task_id, record)
    except ArtifactError as exc:
        raise AppError(status_code=409, code=exc.code, message=exc.message) from exc
    filename = record.path.rsplit("/", 1)[-1]
    return Response(
        content=content,
        media_type=record.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-SHA256": record.sha256,
            "Cache-Control": "private, no-store",
        },
    )
