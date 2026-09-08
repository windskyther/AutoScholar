import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from fastembed import TextEmbedding
from openai import AsyncOpenAI


class EmbeddingError(Exception):
    pass


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, query: str) -> list[float]: ...

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
