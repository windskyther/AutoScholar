from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.rag.models import DocumentChunkRecord
from autoscholar.rag.repository import DuplicateDocumentError, KnowledgeRepository


async def test_repository_persists_projects_documents_and_jobs() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = KnowledgeRepository(async_sessionmaker(engine, expire_on_commit=False))

    project = await repository.create_project(name="LoRA Study", description="Adapters")
    document = await repository.create_document(
        document_id="document-1",
        project_id=project.id,
        original_filename="paper.pdf",
        title="Paper",
        content_type="application/pdf",
        storage_key="document-1.pdf",
        sha256="a" * 64,
        size_bytes=100,
    )

    assert document.status == "queued"
    loaded = await repository.get_document(project.id, document.id)
    assert loaded == document
    projects, project_total = await repository.list_projects(limit=10, offset=0)
    documents, document_total = await repository.list_documents(
        project.id, limit=10, offset=0
    )
    assert projects == [project]
    assert project_total == 1
    assert documents == [document]
    assert document_total == 1

    job = await repository.claim_job(lease_seconds=60)
    assert job is not None
    assert job.document_id == document.id
    assert job.kind == "ingest"
    assert job.status == "processing"
    assert job.attempts == 1

    chunk = DocumentChunkRecord(
        id="chunk-1",
        document_id=document.id,
        project_id=project.id,
        ordinal=0,
        page=1,
        section="Abstract",
        content="Low-rank adaptation",
        token_count=3,
        created_at=datetime.now(UTC),
    )
    await repository.replace_chunks(
        document.id,
        [chunk],
        page_count=4,
        embedding_model="test-model",
        index_version=1,
    )
    await repository.complete_job(job.id)
    ready = await repository.get_document(project.id, document.id)
    assert ready is not None
    assert ready.status == "ready"
    assert ready.page_count == 4
    assert ready.chunk_count == 1

    with pytest.raises(DuplicateDocumentError):
        await repository.create_document(
            document_id="document-2",
            project_id=project.id,
            original_filename="copy.pdf",
            title="Copy",
            content_type="application/pdf",
            storage_key="document-2.pdf",
            sha256="a" * 64,
            size_bytes=100,
        )

    await engine.dispose()


def test_repository_module_does_not_depend_on_local_files() -> None:
    assert not Path("document-1.pdf").exists()
