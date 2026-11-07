"""Read-only response schemas for preserved historical artifacts."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
