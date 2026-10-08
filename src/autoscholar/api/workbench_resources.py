"""Bounded, explicitly projected resources; never export a workspace directory."""

import math
import re
from contextlib import suppress
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from autoscholar.agent.database_models import ArtifactRow, EvidenceRow, ExperimentRow
from autoscholar.agent.records import ArtifactRecord
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.errors import AppError
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification, ModelMetrics

PREVIEW_BYTES = 256 * 1024
DOWNLOAD_BYTES = 16 * 1024 * 1024


class ResourcePage(BaseModel):
    task_id: str
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class ArtifactPreview(BaseModel):
    task_id: str
    artifact_id: str
    sha256: str
    text: str
    truncated: bool


def safe_url(value: str) -> str | None:
    if len(value) > 2048 or any(ord(char) < 33 for char in value):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
        ):
            return value
    except ValueError:
        pass
    return None


def evidence_view(row: EvidenceRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "citation_key": row.citation_key[:16],
        "source_type": row.source_type,
        "title": row.title[:2048],
        "url": safe_url(row.url),
        "authors": [author[:200] for author in row.authors[:30]],
        "year": row.year,
        "claim": row.claim[:8192],
        "excerpt": row.excerpt[:8192],
        "text_truncated": any(
            len(value) > cap
            for value, cap in ((row.title, 2048), (row.claim, 8192), (row.excerpt, 8192))
        ),
        "document_id": row.document_id,
        "page": row.page,
        "section": row.section[:500] if row.section else None,
    }


def experiment_view(row: ExperimentRow) -> dict[str, Any]:
    specification = None
    if row.specification:
        fields = ExperimentSpecification.model_fields
        with suppress(ValidationError):
            specification = ExperimentSpecification.model_validate(
                {key: value for key, value in row.specification.items() if key in fields}
            ).model_dump()
    runs = []
    raw_runs = row.metrics.get("runs", [])
    if isinstance(raw_runs, list):
        for raw in raw_runs[:2]:
            if not isinstance(raw, dict):
                continue
            try:
                runs.append(
                    ModelMetrics.model_validate(
                        {
                            key: value
                            for key, value in raw.items()
                            if key in ModelMetrics.model_fields
                        }
                    ).model_dump()
                )
            except ValidationError:
                continue
    delta = row.metrics.get("cnn_minus_mlp_accuracy")
    winner = row.metrics.get("winner")
    return {
        "id": row.id,
        "task_id": row.task_id,
        "name": row.name[:200],
        "status": row.status,
        "specification": specification,
        "runs": runs,
        "winner": winner if winner in ("mlp", "cnn", "tie") else None,
        "accuracy_delta": delta
        if isinstance(delta, (int, float))
        and not isinstance(delta, bool)
        and math.isfinite(delta)
        and -1 <= delta <= 1
        else None,
        "error_code": row.error_code,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


def artifact_view(row: ArtifactRow) -> dict[str, Any]:
    kind, media = ArtifactManager.allowed[row.path]
    return {
        "id": row.id,
        "task_id": row.task_id,
        "experiment_id": row.experiment_id,
        "type": kind,
        "path": row.path,
        "media_type": media,
        "size_bytes": row.size_bytes,
        "sha256": row.sha256,
        "created_at": row.created_at,
        "previewable": kind != "checkpoint",
    }


class ResourceRepository:
    def __init__(self, repository: WorkbenchRepository) -> None:
        self.repository = repository

    async def page(
        self,
        task_id: str,
        kind: Literal["evidence", "experiments", "artifacts"],
        *,
        limit: int,
        offset: int,
    ) -> ResourcePage:
        model: Any = {
            "evidence": EvidenceRow,
            "experiments": ExperimentRow,
            "artifacts": ArtifactRow,
        }[kind]
        async with self.repository.sessions() as session:
            root = await self.repository.root(session, task_id)
            conditions = [model.task_id.in_(self.repository.family(root))]
            if model is ArtifactRow:
                conditions.append(ArtifactRow.path.in_(tuple(ArtifactManager.allowed)))
            total = await session.scalar(select(func.count()).select_from(model).where(*conditions))
            rows = (
                await session.scalars(
                    select(model)
                    .where(*conditions)
                    .order_by(model.created_at.desc(), model.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            items = []
            for row in rows:
                if isinstance(row, EvidenceRow):
                    items.append(evidence_view(row))
                elif isinstance(row, ExperimentRow):
                    items.append(experiment_view(row))
                else:
                    assert isinstance(row, ArtifactRow)
                    items.append(artifact_view(row))
        return ResourcePage(
            task_id=task_id, items=items, total=int(total or 0), limit=limit, offset=offset
        )

    async def artifact(self, task_id: str, artifact_id: str) -> ArtifactRecord:
        async with self.repository.sessions() as session:
            root = await self.repository.root(session, task_id)
            row = await session.scalar(
                select(ArtifactRow).where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.task_id.in_(self.repository.family(root)),
                    ArtifactRow.path.in_(tuple(ArtifactManager.allowed)),
                )
            )
            if row is None:
                raise AppError(
                    status_code=404,
                    code="workbench_artifact_not_found",
                    message="Registered artifact was not found",
                )
            if not 0 < row.size_bytes <= DOWNLOAD_BYTES or not re.fullmatch(
                r"[0-9a-f]{64}", row.sha256
            ):
                raise AppError(
                    status_code=409,
                    code="artifact_manifest_invalid",
                    message="Artifact manifest is invalid",
                )
            # Preserve the original task owner for workspace verification.
            return AgentTaskRepository._artifact_record(row)


def preview(record: ArtifactRecord, content: bytes, root_id: str) -> ArtifactPreview:
    if ArtifactManager.allowed[record.path][0] not in {
        "report",
        "metrics",
        "experiment",
        "stdout",
        "stderr",
    }:
        raise AppError(
            status_code=415,
            code="artifact_preview_unsupported",
            message="Use verified binary download for this artifact",
        )
    try:
        # Validate all bytes, even when only the bounded prefix is displayed.
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AppError(
            status_code=409, code="artifact_text_invalid", message="Artifact is not UTF-8 text"
        ) from exc
    return ArtifactPreview(
        task_id=root_id,
        artifact_id=record.id,
        sha256=record.sha256,
        text=content[:PREVIEW_BYTES].decode("utf-8", errors="ignore"),
        truncated=len(content) > PREVIEW_BYTES,
    )
