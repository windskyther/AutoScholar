from collections.abc import Sequence
from typing import Protocol

from qdrant_client import AsyncQdrantClient, models

from autoscholar.rag.models import DocumentChunkRecord, DocumentRecord, RetrievedChunk


class ChunkIndex(Protocol):
    async def ensure_collection(self) -> None: ...

    async def replace_document(
        self,
        document: DocumentRecord,
        chunks: Sequence[DocumentChunkRecord],
        vectors: Sequence[Sequence[float]],
    ) -> None: ...

    async def delete_document(self, project_id: str, document_id: str) -> None: ...

    async def search_dense(
        self,
        *,
        project_id: str,
        vector: Sequence[float],
        document_ids: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[RetrievedChunk]: ...


class QdrantChunkIndex:
    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        collection: str,
        dense_dimensions: int,
    ) -> None:
        self._client = client
        self._collection = collection
        self._dense_dimensions = dense_dimensions

    async def ensure_collection(self) -> None:
        if await self._client.collection_exists(self._collection):
            return
        await self._client.create_collection(
            collection_name=self._collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=self._dense_dimensions,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
        await self._client.create_payload_index(
            self._collection, "project_id", models.PayloadSchemaType.KEYWORD
        )
        await self._client.create_payload_index(
            self._collection, "document_id", models.PayloadSchemaType.KEYWORD
        )
        await self._client.create_payload_index(
            self._collection, "page", models.PayloadSchemaType.INTEGER
        )
        await self._client.create_payload_index(
            self._collection, "index_version", models.PayloadSchemaType.INTEGER
        )

    async def replace_document(
        self,
        document: DocumentRecord,
        chunks: Sequence[DocumentChunkRecord],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("Chunk and embedding counts do not match")
        await self.ensure_collection()
        await self.delete_document(document.project_id, document.id)
        points = [
            models.PointStruct(
                id=chunk.id,
                vector={"dense": list(vector)},
                payload={
                    "project_id": chunk.project_id,
                    "document_id": chunk.document_id,
                    "title": document.title,
                    "page": chunk.page,
                    "section": chunk.section,
                    "content": chunk.content,
                    "ordinal": chunk.ordinal,
                    "index_version": 1,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(points), 64):
            await self._client.upsert(
                collection_name=self._collection,
                points=points[start : start + 64],
                wait=True,
            )

    async def delete_document(self, project_id: str, document_id: str) -> None:
        if not await self._client.collection_exists(self._collection):
            return
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="project_id", match=models.MatchValue(value=project_id)
                    ),
                    models.FieldCondition(
                        key="document_id", match=models.MatchValue(value=document_id)
                    ),
                ]
            ),
            wait=True,
        )

    async def search_dense(
        self,
        *,
        project_id: str,
        vector: Sequence[float],
        document_ids: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[RetrievedChunk]:
        must: list[models.Condition] = [
            models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id))
        ]
        if document_ids is not None:
            if not document_ids:
                return []
            must.append(
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchAny(any=list(dict.fromkeys(document_ids))),
                )
            )
        if not await self._client.collection_exists(self._collection):
            return []
        response = await self._client.query_points(
            collection_name=self._collection,
            query=list(vector),
            using="dense",
            query_filter=models.Filter(must=must),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        chunks: list[RetrievedChunk] = []
        for point in response.points:
            payload = point.payload or {}
            chunks.append(
                RetrievedChunk(
                    id=str(point.id),
                    project_id=str(payload.get("project_id") or ""),
                    document_id=str(payload.get("document_id") or ""),
                    title=str(payload.get("title") or "Untitled document"),
                    page=int(payload.get("page") or 0),
                    section=(str(payload["section"]) if payload.get("section") else None),
                    content=str(payload.get("content") or ""),
                    score=float(point.score),
                    ordinal=int(payload.get("ordinal") or 0),
                )
            )
        return chunks
