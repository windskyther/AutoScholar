from dataclasses import dataclass
from datetime import datetime
from typing import Literal

DocumentStatus = Literal["queued", "processing", "ready", "failed", "deleting"]
DocumentJobKind = Literal["ingest", "reindex", "delete"]
DocumentJobStatus = Literal["queued", "processing", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: str
    project_id: str
    original_filename: str
    title: str
    content_type: str
    storage_key: str
    sha256: str
    size_bytes: int
    status: DocumentStatus
    page_count: int | None
    chunk_count: int
    embedding_model: str | None
    index_version: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentChunkRecord:
    id: str
    document_id: str
    project_id: str
    ordinal: int
    page: int
    section: str | None
    content: str
    token_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentJobRecord:
    id: str
    document_id: str
    kind: DocumentJobKind
    status: DocumentJobStatus
    attempts: int
    available_at: datetime
    lease_expires_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
