import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from fastembed import SparseTextEmbedding, TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from openai import AsyncOpenAI

from autoscholar.rag.models import RetrievedChunk


class EmbeddingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SparseVectorData:
    indices: list[int]
    values: list[float]


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, query: str) -> list[float]: ...

    async def close(self) -> None: ...


class SparseEmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def embed_documents(self, documents: Sequence[str]) -> list[SparseVectorData]: ...

    async def embed_query(self, query: str) -> SparseVectorData: ...

    async def close(self) -> None: ...


class Reranker(Protocol):
    @property
    def model(self) -> str: ...

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]: ...

    async def close(self) -> None: ...


class FastEmbedProvider:
    def __init__(self, *, model: str, dimensions: int, cache_dir: Path) -> None:
        self._model_name = model
        self._dimensions = dimensions
        self._cache_dir = cache_dir
        self._embedding: TextEmbedding | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        if not documents:
            return []
        embedding = await self._get_embedding()
        vectors = await asyncio.to_thread(
            lambda: [vector.tolist() for vector in embedding.embed(list(documents))]
        )
        self._validate(vectors)
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        embedding = await self._get_embedding()
        vectors = await asyncio.to_thread(
            lambda: [vector.tolist() for vector in embedding.query_embed(query)]
        )
        self._validate(vectors)
        return cast(list[float], vectors[0])

    async def close(self) -> None:
        return None

    async def _get_embedding(self) -> TextEmbedding:
        if self._embedding is not None:
            return self._embedding
        async with self._load_lock:
            if self._embedding is None:
                self._embedding = await asyncio.to_thread(
                    TextEmbedding,
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir),
                    lazy_load=True,
                )
        return self._embedding

    def _validate(self, vectors: Sequence[Sequence[float]]) -> None:
        if any(len(vector) != self._dimensions for vector in vectors):
            raise EmbeddingError("Embedding provider returned an unexpected vector size")


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> None:
        self._model_name = model
        self._dimensions = dimensions
        self._client = AsyncOpenAI(
            base_url=base_url.rstrip("/") + "/",
            api_key=api_key,
            timeout=timeout_seconds,
        )

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        if not documents:
            return []
        response = await self._client.embeddings.create(
            input=list(documents),
            model=self._model_name,
            dimensions=self._dimensions,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]
        self._validate(vectors)
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self.embed_documents([query])
        return vectors[0]

    async def close(self) -> None:
        await self._client.close()

    def _validate(self, vectors: Sequence[Sequence[float]]) -> None:
        if any(len(vector) != self._dimensions for vector in vectors):
            raise EmbeddingError("Embedding provider returned an unexpected vector size")


class FastEmbedSparseProvider:
    def __init__(self, *, model: str, cache_dir: Path) -> None:
        self._model_name = model
        self._cache_dir = cache_dir
        self._embedding: SparseTextEmbedding | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model_name

    async def embed_documents(self, documents: Sequence[str]) -> list[SparseVectorData]:
        if not documents:
            return []
        embedding = await self._get_embedding()
        vectors = await asyncio.to_thread(lambda: list(embedding.embed(list(documents))))
        return [
            SparseVectorData(
                indices=[int(value) for value in vector.indices.tolist()],
                values=[float(value) for value in vector.values.tolist()],
            )
            for vector in vectors
        ]

    async def embed_query(self, query: str) -> SparseVectorData:
        embedding = await self._get_embedding()
        vectors = await asyncio.to_thread(lambda: list(embedding.query_embed(query)))
        vector = vectors[0]
        return SparseVectorData(
            indices=[int(value) for value in vector.indices.tolist()],
            values=[float(value) for value in vector.values.tolist()],
        )

    async def close(self) -> None:
        return None

    async def _get_embedding(self) -> SparseTextEmbedding:
        if self._embedding is not None:
            return self._embedding
        async with self._load_lock:
            if self._embedding is None:
                self._embedding = await asyncio.to_thread(
                    SparseTextEmbedding,
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir),
                    lazy_load=True,
                )
        return self._embedding


class FastEmbedReranker:
    def __init__(self, *, model: str, cache_dir: Path) -> None:
        self._model_name = model
        self._cache_dir = cache_dir
        self._reranker: TextCrossEncoder | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model_name

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        reranker = await self._get_reranker()
        scores = await asyncio.to_thread(
            lambda: list(reranker.rerank(query, [chunk.content for chunk in chunks]))
        )
        ranked = sorted(
            (
                replace(chunk, score=float(score))
                for chunk, score in zip(chunks, scores, strict=True)
            ),
            key=lambda chunk: chunk.score,
            reverse=True,
        )
        return ranked[:limit]

    async def close(self) -> None:
        return None

    async def _get_reranker(self) -> TextCrossEncoder:
        if self._reranker is not None:
            return self._reranker
        async with self._load_lock:
            if self._reranker is None:
                self._reranker = await asyncio.to_thread(
                    TextCrossEncoder,
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir),
                    lazy_load=True,
                )
        return self._reranker
