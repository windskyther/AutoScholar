import asyncio
import contextlib
import signal

import structlog

from autoscholar.core.config import Settings, get_settings
from autoscholar.infrastructure import Database, Qdrant
from autoscholar.rag.chunking import StructureAwareChunker
from autoscholar.rag.embeddings import (
    EmbeddingProvider,
    FastEmbedProvider,
    OpenAICompatibleEmbeddingProvider,
)
from autoscholar.rag.index import ChunkIndex, QdrantChunkIndex
from autoscholar.rag.parser import DocumentProcessingError, PDFParser
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.storage import DocumentStorage, LocalDocumentStorage

logger = structlog.get_logger(__name__)


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "fastembed":
        return FastEmbedProvider(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            cache_dir=settings.model_cache_path,
        )
    if not settings.embedding_api_key:
        raise RuntimeError("EMBEDDING_API_KEY is required for openai_compatible embeddings")
    return OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.llm_timeout_seconds,
    )


class DocumentWorker:
    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        storage: DocumentStorage,
        parser: PDFParser,
        chunker: StructureAwareChunker,
        embeddings: EmbeddingProvider,
        index: ChunkIndex,
        lease_seconds: int,
        max_attempts: int,
        parse_timeout_seconds: int,
        max_pages: int,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._parser = parser
        self._chunker = chunker
        self._embeddings = embeddings
        self._index = index
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._parse_timeout_seconds = parse_timeout_seconds
        self._max_pages = max_pages

    async def run_once(self) -> bool:
        job = await self._repository.claim_job(lease_seconds=self._lease_seconds)
        if job is None:
            return False
        document = await self._repository.get_document_by_id(job.document_id)
        if document is None:
            await self._repository.complete_job(job.id)
            return True
        try:
            if job.kind == "delete":
                await self._index.delete_document(document.project_id, document.id)
                self._storage.delete(document.storage_key)
                await self._repository.delete_document_record(document.id)
                return True
            parsed = await asyncio.wait_for(
                asyncio.to_thread(
                    self._parser.parse,
                    self._storage.path_for(document.storage_key),
                    max_pages=self._max_pages,
                ),
                timeout=self._parse_timeout_seconds,
            )
            chunks = self._chunker.chunk(
                parsed, document_id=document.id, project_id=document.project_id
            )
            if not chunks:
                raise DocumentProcessingError(
                    "pdf_text_not_extractable", "PDF did not produce searchable text chunks"
                )
            vectors = await self._embeddings.embed_documents(
                [chunk.content for chunk in chunks]
            )
            await self._index.replace_document(document, chunks, vectors)
            await self._repository.replace_chunks(
                document.id,
                chunks,
                page_count=parsed.page_count,
                embedding_model=self._embeddings.model,
                index_version=1,
            )
            await self._repository.complete_job(job.id)
        except DocumentProcessingError as exc:
            await self._repository.fail_job(
                job.id, code=exc.code, message=exc.message, retry=False
            )
        except TimeoutError:
            await self._fail_transient(job.id, job.attempts, "document_parse_timeout")
        except Exception:
            logger.exception("document_job_failed", job_id=job.id, document_id=document.id)
            await self._fail_transient(job.id, job.attempts, "document_processing_failed")
        return True

    async def _fail_transient(self, job_id: str, attempts: int, code: str) -> None:
        retry = attempts < self._max_attempts
        await self._repository.fail_job(
            job_id,
            code=code,
            message="Document processing failed; inspect worker logs for details",
            retry=retry,
            retry_delay_seconds=30 * (2 ** max(attempts - 1, 0)),
        )


async def run_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    database = Database(settings.database_url)
    qdrant_key = (
        settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    )
    qdrant = Qdrant(settings.qdrant_url, api_key=qdrant_key)
    embeddings = create_embedding_provider(settings)
    worker = DocumentWorker(
        repository=KnowledgeRepository(database.session_factory),
        storage=LocalDocumentStorage(settings.document_storage_path),
        parser=PDFParser(),
        chunker=StructureAwareChunker(
            max_tokens=settings.rag_chunk_tokens,
            overlap_tokens=settings.rag_chunk_overlap,
        ),
        embeddings=embeddings,
        index=QdrantChunkIndex(
            qdrant.client,
            collection=settings.qdrant_collection,
            dense_dimensions=embeddings.dimensions,
        ),
        lease_seconds=settings.rag_job_lease_seconds,
        max_attempts=settings.rag_job_max_attempts,
        parse_timeout_seconds=settings.document_parse_timeout_seconds,
        max_pages=settings.document_max_pages,
    )
    try:
        while True:
            if stop is not None and stop.is_set():
                return
            handled = await worker.run_once()
            if handled:
                continue
            if stop is None:
                await asyncio.sleep(settings.rag_worker_poll_seconds)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.rag_worker_poll_seconds
                )
    finally:
        await embeddings.close()
        await qdrant.close()
        await database.close()


async def _main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signame, stop.set)

    await run_worker(get_settings(), stop=stop)


if __name__ == "__main__":
    asyncio.run(_main())
