"""One disposable native process owns all local model weights and the vector server."""

import asyncio
import multiprocessing
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.core.errors import AppError
from autoscholar.desktop.storage import DesktopDatabase
from autoscholar.evidence.index import EvidenceIndex
from autoscholar.evidence.store import EvidenceStore
from autoscholar.infrastructure import Qdrant
from autoscholar.llm import LLMProvider
from autoscholar.llm.factory import UnconfiguredLLMProvider
from autoscholar.rag import (
    FastEmbedProvider,
    FastEmbedReranker,
    FastEmbedSparseProvider,
    KnowledgeRepository,
    LocalDocumentStorage,
    PDFParser,
    QdrantChunkIndex,
    RAGQueryService,
    RetrievalMode,
    RetrievedChunk,
)
from autoscholar.rag.chunking import StructureAwareChunker
from autoscholar.rag.service import RAGAnswerService, RAGTaskStore
from autoscholar.rag.worker import DocumentWorker


class ModelWorker:
    def __init__(self, folder: Path, executable: Path) -> None:
        self.folder, self.executable = folder, executable
        self.server: subprocess.Popen[bytes] | None = None
        self.loop = asyncio.new_event_loop()

    async def start(self) -> None:
        directory = self.folder / "vectors"
        directory.mkdir(exist_ok=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(32)
        url = f"http://127.0.0.1:{port}"
        self.server = subprocess.Popen(
            [self.executable],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
            env={
                **os.environ,
                "QDRANT__SERVICE__HOST": "127.0.0.1",
                "QDRANT__SERVICE__HTTP_PORT": str(port),
                "QDRANT__SERVICE__GRPC_PORT": "0",
                "QDRANT__SERVICE__API_KEY": token,
                "QDRANT__TELEMETRY_DISABLED": "true",
            },
        )
        async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
            for _ in range(100):
                if self.server.poll() is not None:
                    raise RuntimeError("Native Qdrant failed to start")
                try:
                    if (await client.get("/readyz", headers={"api-key": token})).is_success:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("Native Qdrant startup timed out")

        from autoscholar.desktop.runtime import desktop_settings

        settings = desktop_settings(self.folder, SecretStr("retrieval-process"))
        db = DesktopDatabase(self.folder / "library.sqlite3")
        qdrant = Qdrant(url, api_key=token)
        cache = settings.model_cache_path
        dense = FastEmbedProvider(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            cache_dir=cache,
        )
        sparse = FastEmbedSparseProvider(model=settings.sparse_model, cache_dir=cache)
        index = QdrantChunkIndex(
            qdrant.client, collection=settings.qdrant_collection, dense_dimensions=dense.dimensions
        )
        knowledge = KnowledgeRepository(db.session_factory)
        self.query = RAGQueryService(
            provider=UnconfiguredLLMProvider(),
            embeddings=dense,
            index=index,
            knowledge=knowledge,
            tasks=AgentTaskRepository(db.session_factory),
            default_top_k=settings.rag_top_k,
            sparse_embeddings=sparse,
            reranker=FastEmbedReranker(model=settings.reranker_model, cache_dir=cache),
            candidate_limit=settings.rag_candidate_limit,
        )
        self.documents = DocumentWorker(
            repository=knowledge,
            storage=LocalDocumentStorage(settings.document_storage_path),
            parser=PDFParser(),
            chunker=StructureAwareChunker(
                max_tokens=settings.rag_chunk_tokens, overlap_tokens=settings.rag_chunk_overlap
            ),
            embeddings=dense,
            index=index,
            lease_seconds=settings.rag_job_lease_seconds,
            max_attempts=settings.rag_job_max_attempts,
            parse_timeout_seconds=settings.document_parse_timeout_seconds,
            max_pages=settings.document_max_pages,
            sparse_embeddings=sparse,
        )
        self.evidence = EvidenceIndex(
            EvidenceStore(db.session_factory), qdrant.client, dense, settings.qdrant_collection
        )

    def close(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=10)


_worker: ModelWorker | None = None


def _execute(folder: Path, executable: Path, method: str, params: dict[str, Any]) -> Any:
    import atexit

    global _worker
    if _worker is None:
        candidate = ModelWorker(folder, executable)
        try:
            candidate.loop.run_until_complete(candidate.start())
        except Exception:
            candidate.close()
            raise
        _worker = candidate
        atexit.register(candidate.close)
    handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {
        "retrieve": _worker.query.retrieve,
        "document": _worker.documents.run_once,
        "evidence": _worker.evidence.run_once,
        "search": _worker.evidence.search,
    }
    try:
        return True, _worker.loop.run_until_complete(handlers[method](**params))
    except AppError as exc:
        return False, (exc.status_code, exc.code, exc.message)


class Retrieval:
    def __init__(self, folder: Path, executable: Path) -> None:
        self.folder, self.executable = folder, executable
        self.pool: ProcessPoolExecutor | None = None
        self.pending = 0
        self.last_used = 0.0
        self.cache_available = asyncio.Event()
        self.cache_available.set()

    async def call(self, method: str, **params: Any) -> Any:
        while not self.cache_available.is_set():
            await self.cache_available.wait()
        if self.pool is None:
            self.pool = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))
        future = asyncio.get_running_loop().run_in_executor(
            self.pool, _execute, self.folder, self.executable, method, params
        )
        self.pending += 1

        def completed(_: object) -> None:
            self.pending -= 1
            self.last_used = time.monotonic()

        future.add_done_callback(completed)
        try:
            ok, result = await asyncio.shield(future)
        except BrokenProcessPool:
            self.close()
            raise
        if not ok:
            status, code, message = result
            raise AppError(status_code=status, code=code, message=message)
        return result

    async def search(self, project_id: str, query: str, *, limit: int) -> dict[str, float]:
        return dict(await self.call("search", project_id=project_id, query=query, limit=limit))

    def close(self) -> None:
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None

    def reap(self) -> None:
        idle = not self.pending and time.monotonic() - self.last_used >= 60
        if idle and self.cache_available.is_set():
            self.close()

    async def clear_model_cache(self) -> bool:
        if self.pending or not self.cache_available.is_set():
            return False

        def remove() -> None:
            cache = self.folder / "models"
            if cache.is_symlink() or cache.is_junction():
                raise OSError("Model cache is a filesystem link")
            if cache.resolve() != self.folder.resolve() / "models":
                raise OSError("Model cache points outside its designated directory")
            self.close()  # Release mapped model files before removing them on Windows.
            if cache.exists():
                shutil.rmtree(cache)

        self.cache_available.clear()
        removal = asyncio.create_task(asyncio.to_thread(remove))
        # A disconnected client must not allow a new download while removal is in progress.
        removal.add_done_callback(lambda _: self.cache_available.set())
        await asyncio.shield(removal)
        return True


class DesktopQuery(RAGAnswerService):
    def __init__(self, provider: LLMProvider, tasks: RAGTaskStore, retrieval: Retrieval) -> None:
        super().__init__(provider, tasks)
        self.retrieval = retrieval

    async def retrieve(
        self,
        question: str,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        retrieval_mode: RetrievalMode = "hybrid_rerank",
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        return list(
            await self.retrieval.call(
                "retrieve",
                question=question,
                project_id=project_id,
                document_ids=document_ids,
                retrieval_mode=retrieval_mode,
                top_k=top_k,
            )
        )
