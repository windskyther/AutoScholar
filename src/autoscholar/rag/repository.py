from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.rag.database_models import (
    DocumentChunkRow,
    DocumentJobRow,
    DocumentRow,
    ProjectRow,
)
from autoscholar.rag.models import (
    DocumentChunkRecord,
    DocumentJobKind,
    DocumentJobRecord,
    DocumentJobStatus,
    DocumentRecord,
    DocumentStatus,
    ProjectRecord,
)


class DuplicateDocumentError(Exception):
    pass


class KnowledgeStore(Protocol):
    async def create_project(self, *, name: str, description: str | None) -> ProjectRecord: ...

    async def get_project(self, project_id: str) -> ProjectRecord | None: ...

    async def list_projects(
        self, *, limit: int, offset: int
    ) -> tuple[list[ProjectRecord], int]: ...

    async def create_document(
        self,
        *,
        document_id: str,
        project_id: str,
        original_filename: str,
        title: str,
        content_type: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
    ) -> DocumentRecord: ...

    async def get_document(
        self, project_id: str, document_id: str
    ) -> DocumentRecord | None: ...

    async def list_documents(
        self, project_id: str, *, limit: int, offset: int
    ) -> tuple[list[DocumentRecord], int]: ...

    async def enqueue_document_job(
        self, document_id: str, *, kind: DocumentJobKind
    ) -> DocumentRecord: ...


class KnowledgeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create_project(self, *, name: str, description: str | None) -> ProjectRecord:
        async with self._sessions() as session:
            row = ProjectRow(id=str(uuid4()), name=name, description=description)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._project_record(row)

    async def get_project(self, project_id: str) -> ProjectRecord | None:
        async with self._sessions() as session:
            row = await session.get(ProjectRow, project_id)
            return self._project_record(row) if row is not None else None

    async def list_projects(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[ProjectRecord], int]:
        async with self._sessions() as session:
            total = int(await session.scalar(select(func.count(ProjectRow.id))) or 0)
            result = await session.execute(
                select(ProjectRow)
                .order_by(ProjectRow.created_at, ProjectRow.id)
                .limit(limit)
                .offset(offset)
            )
            return [self._project_record(row) for row in result.scalars()], total

    async def create_document(
        self,
        *,
        document_id: str,
        project_id: str,
        original_filename: str,
        title: str,
        content_type: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
    ) -> DocumentRecord:
        async with self._sessions() as session:
            row = DocumentRow(
                id=document_id,
                project_id=project_id,
                original_filename=original_filename,
                title=title,
                content_type=content_type,
                storage_key=storage_key,
                sha256=sha256,
                size_bytes=size_bytes,
                status="queued",
                chunk_count=0,
                index_version=0,
            )
            row.jobs.append(
                DocumentJobRow(
                    id=str(uuid4()),
                    kind="ingest",
                    status="queued",
                    attempts=0,
                )
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise DuplicateDocumentError from exc
            await session.refresh(row)
            return self._document_record(row)

    async def get_document(
        self, project_id: str, document_id: str
    ) -> DocumentRecord | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(DocumentRow).where(
                    DocumentRow.id == document_id,
                    DocumentRow.project_id == project_id,
                )
            )
            row = result.scalar_one_or_none()
            return self._document_record(row) if row is not None else None

    async def get_document_by_id(self, document_id: str) -> DocumentRecord | None:
        async with self._sessions() as session:
            row = await session.get(DocumentRow, document_id)
            return self._document_record(row) if row is not None else None

    async def list_documents(
        self, project_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[DocumentRecord], int]:
        async with self._sessions() as session:
            predicate = DocumentRow.project_id == project_id
            total = int(
                await session.scalar(select(func.count(DocumentRow.id)).where(predicate)) or 0
            )
            result = await session.execute(
                select(DocumentRow)
                .where(predicate)
                .order_by(DocumentRow.created_at, DocumentRow.id)
                .limit(limit)
                .offset(offset)
            )
            return [self._document_record(row) for row in result.scalars()], total

    async def enqueue_document_job(
        self, document_id: str, *, kind: DocumentJobKind
    ) -> DocumentRecord:
        async with self._sessions() as session:
            row = await session.get(DocumentRow, document_id)
            if row is None:
                raise LookupError(f"Unknown document: {document_id}")
            row.status = "deleting" if kind == "delete" else "queued"
            row.error_code = None
            row.error_message = None
            session.add(
                DocumentJobRow(
                    id=str(uuid4()),
                    document_id=document_id,
                    kind=kind,
                    status="queued",
                    attempts=0,
                )
            )
            await session.commit()
            await session.refresh(row)
            return self._document_record(row)

    async def claim_job(self, *, lease_seconds: int) -> DocumentJobRecord | None:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            result = await session.execute(
                select(DocumentJobRow)
                .where(
                    or_(
                        (DocumentJobRow.status == "queued")
                        & (DocumentJobRow.available_at <= now),
                        (DocumentJobRow.status == "processing")
                        & (DocumentJobRow.lease_expires_at < now),
                    )
                )
                .order_by(DocumentJobRow.available_at, DocumentJobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            document = await session.get(DocumentRow, row.document_id)
            if document is not None and row.kind != "delete":
                document.status = "processing"
            await session.commit()
            await session.refresh(row)
            return self._job_record(row)

    async def replace_chunks(
        self,
        document_id: str,
        chunks: Sequence[DocumentChunkRecord],
        *,
        page_count: int,
        embedding_model: str,
        index_version: int,
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(DocumentRow, document_id)
            if row is None:
                raise LookupError(f"Unknown document: {document_id}")
            await session.execute(
                delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
            )
            session.add_all(
                [
                    DocumentChunkRow(
                        id=item.id,
                        document_id=item.document_id,
                        project_id=item.project_id,
                        ordinal=item.ordinal,
                        page=item.page,
                        section=item.section,
                        content=item.content,
                        token_count=item.token_count,
                    )
                    for item in chunks
                ]
            )
            row.page_count = page_count
            row.chunk_count = len(chunks)
            row.embedding_model = embedding_model
            row.index_version = index_version
            row.status = "ready"
            row.error_code = None
            row.error_message = None
            await session.commit()

    async def complete_job(self, job_id: str) -> None:
        async with self._sessions() as session:
            row = await session.get(DocumentJobRow, job_id)
            if row is None:
                raise LookupError(f"Unknown document job: {job_id}")
            row.status = "succeeded"
            row.lease_expires_at = None
            await session.commit()

    async def fail_job(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> None:
        async with self._sessions() as session:
            row = await session.get(DocumentJobRow, job_id)
            if row is None:
                raise LookupError(f"Unknown document job: {job_id}")
            row.error_code = code
            row.error_message = message
            row.lease_expires_at = None
            row.status = "queued" if retry else "failed"
            if retry:
                row.available_at = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds)
            document = await session.get(DocumentRow, row.document_id)
            if document is not None:
                document.status = "queued" if retry else "failed"
                document.error_code = code
                document.error_message = message
            await session.commit()

    async def delete_document_record(self, document_id: str) -> None:
        async with self._sessions() as session:
            row = await session.get(DocumentRow, document_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    @staticmethod
    def _project_record(row: ProjectRow) -> ProjectRecord:
        return ProjectRecord(
            id=row.id,
            name=row.name,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _document_record(row: DocumentRow) -> DocumentRecord:
        return DocumentRecord(
            id=row.id,
            project_id=row.project_id,
            original_filename=row.original_filename,
            title=row.title,
            content_type=row.content_type,
            storage_key=row.storage_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            status=cast(DocumentStatus, row.status),
            page_count=row.page_count,
            chunk_count=row.chunk_count,
            embedding_model=row.embedding_model,
            index_version=row.index_version,
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _job_record(row: DocumentJobRow) -> DocumentJobRecord:
        return DocumentJobRecord(
            id=row.id,
            document_id=row.document_id,
            kind=cast(DocumentJobKind, row.kind),
            status=cast(DocumentJobStatus, row.status),
            attempts=row.attempts,
            available_at=row.available_at,
            lease_expires_at=row.lease_expires_at,
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
