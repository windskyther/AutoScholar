from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from autoscholar.rag.chunking import StructureAwareChunker
from autoscholar.rag.embeddings import EmbeddingProvider, SparseVectorData
from autoscholar.rag.index import ChunkIndex
from autoscholar.rag.models import (
    DocumentChunkRecord,
    DocumentJobRecord,
    DocumentRecord,
)
from autoscholar.rag.parser import (
    DocumentProcessingError,
    ParsedDocument,
    ParsedPage,
    PDFParser,
)
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.storage import LocalDocumentStorage
from autoscholar.rag.worker import DocumentWorker


def document_record() -> DocumentRecord:
    now = datetime.now(UTC)
    return DocumentRecord(
        id="document-1",
        project_id="project-1",
        original_filename="paper.pdf",
        title="Paper",
        content_type="application/pdf",
        storage_key="document-1.pdf",
        sha256="a" * 64,
        size_bytes=100,
        status="queued",
        page_count=None,
        chunk_count=0,
        embedding_model=None,
        index_version=0,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def job_record() -> DocumentJobRecord:
    now = datetime.now(UTC)
    return DocumentJobRecord(
        id="job-1",
        document_id="document-1",
        kind="ingest",
        status="processing",
        attempts=1,
        available_at=now,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


class FakeWorkerRepository:
    def __init__(self) -> None:
        self.job: DocumentJobRecord | None = job_record()
        self.document = document_record()
        self.replaced: list[DocumentChunkRecord] = []
        self.completed = False
        self.failure: tuple[str, bool] | None = None

    async def claim_job(self, *, lease_seconds: int) -> DocumentJobRecord | None:
        del lease_seconds
        job, self.job = self.job, None
        return job

    async def get_document_by_id(self, document_id: str) -> DocumentRecord | None:
        return self.document if document_id == self.document.id else None

    async def replace_chunks(
        self,
        document_id: str,
        chunks: list[DocumentChunkRecord],
        *,
        page_count: int,
        embedding_model: str,
        index_version: int,
    ) -> None:
        assert document_id == self.document.id
        assert page_count == 1
        assert embedding_model == "test-embedding"
        assert index_version == 1
        self.replaced = chunks

    async def complete_job(self, job_id: str) -> None:
        assert job_id == "job-1"
        self.completed = True

    async def fail_job(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retry: bool,
        retry_delay_seconds: int = 0,
    ) -> None:
        del job_id, message, retry_delay_seconds
        self.failure = (code, retry)

    async def delete_document_record(self, document_id: str) -> None:
        del document_id


class SuccessfulParser(PDFParser):
    def parse(self, path: Path, *, max_pages: int) -> ParsedDocument:
        del path, max_pages
        return ParsedDocument(
            pages=(
                ParsedPage(
                    page=1,
                    text="METHOD\n" + "low rank adaptation " * 40,
                ),
            ),
            page_count=1,
        )


class FailingParser(PDFParser):
    def parse(self, path: Path, *, max_pages: int) -> ParsedDocument:
        del path, max_pages
        raise DocumentProcessingError("encrypted_pdf", "Encrypted PDF")


class FakeEmbeddings:
    model = "test-embedding"
    dimensions = 2

    async def embed_documents(self, documents: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in documents]

    async def embed_query(self, query: str) -> list[float]:
        del query
        return [1.0, 0.0]

    async def close(self) -> None:
        return None


class FakeIndex:
    def __init__(self) -> None:
        self.indexed = 0

    async def ensure_collection(self) -> None:
        return None

    async def replace_document(
        self,
        document: DocumentRecord,
        chunks: list[DocumentChunkRecord],
        vectors: list[list[float]],
        sparse_vectors: list[SparseVectorData] | None = None,
    ) -> None:
        del document, sparse_vectors
        assert len(chunks) == len(vectors)
        self.indexed = len(chunks)

    async def delete_document(self, project_id: str, document_id: str) -> None:
        del project_id, document_id


def worker(
    repository: FakeWorkerRepository,
    storage: LocalDocumentStorage,
    parser: PDFParser,
    index: FakeIndex,
) -> DocumentWorker:
    return DocumentWorker(
        repository=cast(KnowledgeRepository, repository),
        storage=storage,
        parser=parser,
        chunker=StructureAwareChunker(max_tokens=12, overlap_tokens=2),
        embeddings=cast(EmbeddingProvider, FakeEmbeddings()),
        index=cast(ChunkIndex, index),
        lease_seconds=60,
        max_attempts=3,
        parse_timeout_seconds=5,
        max_pages=10,
    )


async def test_worker_indexes_and_persists_document(tmp_path: Path) -> None:
    repository = FakeWorkerRepository()
    storage = LocalDocumentStorage(tmp_path)
    storage.path_for("document-1.pdf").write_bytes(b"%PDF-1.4")
    index = FakeIndex()

    handled = await worker(repository, storage, SuccessfulParser(), index).run_once()

    assert handled is True
    assert repository.completed is True
    assert repository.failure is None
    assert repository.replaced
    assert index.indexed == len(repository.replaced)


async def test_worker_does_not_retry_permanent_pdf_errors(tmp_path: Path) -> None:
    repository = FakeWorkerRepository()
    storage = LocalDocumentStorage(tmp_path)
    storage.path_for("document-1.pdf").write_bytes(b"%PDF-1.4")

    await worker(repository, storage, FailingParser(), FakeIndex()).run_once()

    assert repository.completed is False
    assert repository.failure == ("encrypted_pdf", False)
