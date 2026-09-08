from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, StringConstraints

from autoscholar.core.errors import AppError
from autoscholar.rag import (
    DocumentRecord,
    DocumentStorage,
    DocumentStorageError,
    DuplicateDocumentError,
    KnowledgeStore,
    ProjectRecord,
)

router = APIRouter(prefix="/projects", tags=["projects"])
ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ProjectDescription = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
]
DocumentTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]


class ProjectCreateRequest(BaseModel):
    name: ProjectName
    description: ProjectDescription | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    total: int
    limit: int
    offset: int


class DocumentResponse(BaseModel):
    id: str
    project_id: str
    original_filename: str
    title: str
    content_type: str
    sha256: str
    size_bytes: int
    status: str
    page_count: int | None
    chunk_count: int
    embedding_model: str | None
    index_version: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    project_id: str
    items: list[DocumentResponse]
    total: int
    limit: int
    offset: int


def _store(request: Request) -> KnowledgeStore:
    store: KnowledgeStore | None = request.app.state.knowledge_repository
    if store is None:
        raise AppError(
            status_code=503,
            code="knowledge_store_not_available",
            message="Knowledge base storage is unavailable",
        )
    return store


def _project_response(record: ProjectRecord) -> ProjectResponse:
    return ProjectResponse(
        id=record.id,
        name=record.name,
        description=record.description,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _document_response(record: DocumentRecord) -> DocumentResponse:
    payload = {
        name: getattr(record, name)
        for name in DocumentResponse.model_fields
    }
    return DocumentResponse(**payload)


async def _project_or_404(store: KnowledgeStore, project_id: str) -> ProjectRecord:
    project = await store.get_project(project_id)
    if project is None:
        raise AppError(status_code=404, code="project_not_found", message="Project was not found")
    return project


async def _document_or_404(
    store: KnowledgeStore, project_id: str, document_id: str
) -> DocumentRecord:
    document = await store.get_document(project_id, document_id)
    if document is None:
        raise AppError(
            status_code=404, code="document_not_found", message="Document was not found"
        )
    return document


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(payload: ProjectCreateRequest, request: Request) -> ProjectResponse:
    record = await _store(request).create_project(
        name=payload.name, description=payload.description
    )
    return _project_response(record)


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProjectListResponse:
    items, total = await _store(request).list_projects(limit=limit, offset=offset)
    return ProjectListResponse(
        items=[_project_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, request: Request) -> ProjectResponse:
    return _project_response(await _project_or_404(_store(request), project_id))


@router.post("/{project_id}/documents", response_model=DocumentResponse, status_code=202)
async def upload_document(
    project_id: str,
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
    title: Annotated[DocumentTitle | None, Form()] = None,
) -> DocumentResponse:
    store = _store(request)
    await _project_or_404(store, project_id)
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".pdf"):
        raise AppError(
            status_code=415,
            code="unsupported_document_type",
            message="Only PDF files are supported",
        )
    allowed_content_types = {
        "application/pdf",
        "application/x-pdf",
        "application/octet-stream",
    }
    if file.content_type not in allowed_content_types:
        raise AppError(
            status_code=415,
            code="unsupported_document_type",
            message="Only PDF files are supported",
        )
    document_id = str(uuid4())
    storage: DocumentStorage = request.app.state.document_storage
    try:
        stored = storage.save(
            document_id,
            file.file,
            max_bytes=request.app.state.settings.document_max_bytes,
        )
    except DocumentStorageError as exc:
        raise AppError(status_code=422, code=exc.code, message=exc.message) from exc
    try:
        record = await store.create_document(
            document_id=document_id,
            project_id=project_id,
            original_filename=filename,
            title=title or Path(filename).stem,
            content_type="application/pdf",
            storage_key=stored.key,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
        )
    except DuplicateDocumentError as exc:
        storage.delete(stored.key)
        raise AppError(
            status_code=409,
            code="duplicate_document",
            message="This PDF already exists in the project",
        ) from exc
    response.headers["Location"] = f"/projects/{project_id}/documents/{document_id}"
    return _document_response(record)


@router.get("/{project_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    project_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> DocumentListResponse:
    store = _store(request)
    await _project_or_404(store, project_id)
    items, total = await store.list_documents(project_id, limit=limit, offset=offset)
    return DocumentListResponse(
        project_id=project_id,
        items=[_document_response(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{project_id}/documents/{document_id}", response_model=DocumentResponse)
async def get_document(project_id: str, document_id: str, request: Request) -> DocumentResponse:
    return _document_response(await _document_or_404(_store(request), project_id, document_id))


@router.get("/{project_id}/documents/{document_id}/content")
async def get_document_content(
    project_id: str, document_id: str, request: Request
) -> FileResponse:
    document = await _document_or_404(_store(request), project_id, document_id)
    storage: DocumentStorage = request.app.state.document_storage
    path = storage.path_for(document.storage_key)
    if not path.is_file():
        raise AppError(
            status_code=503,
            code="document_content_unavailable",
            message="Document content is unavailable",
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=document.original_filename,
        content_disposition_type="inline",
    )


@router.post("/{project_id}/documents/{document_id}/retry", response_model=DocumentResponse)
async def retry_document(
    project_id: str, document_id: str, request: Request
) -> DocumentResponse:
    store = _store(request)
    document = await _document_or_404(store, project_id, document_id)
    if document.status != "failed":
        raise AppError(
            status_code=409,
            code="document_not_failed",
            message="Only failed documents can be retried",
        )
    return _document_response(await store.enqueue_document_job(document.id, kind="ingest"))


@router.post("/{project_id}/documents/{document_id}/reindex", response_model=DocumentResponse)
async def reindex_document(
    project_id: str, document_id: str, request: Request
) -> DocumentResponse:
    store = _store(request)
    document = await _document_or_404(store, project_id, document_id)
    if document.status != "ready":
        raise AppError(
            status_code=409,
            code="document_not_ready",
            message="Only ready documents can be reindexed",
        )
    return _document_response(await store.enqueue_document_job(document.id, kind="reindex"))


@router.delete("/{project_id}/documents/{document_id}", status_code=202)
async def delete_document(project_id: str, document_id: str, request: Request) -> Response:
    store = _store(request)
    document = await _document_or_404(store, project_id, document_id)
    if document.status == "deleting":
        return Response(status_code=202)
    await store.enqueue_document_job(document.id, kind="delete")
    return Response(status_code=202)
