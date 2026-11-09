"""Rebuildable semantic index using the existing local embedding provider only."""

import asyncio
import hashlib
import time

from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import select

from autoscholar.evidence.database import ProjectClaimRow
from autoscholar.evidence.store import EvidenceStore
from autoscholar.rag.embeddings import FastEmbedProvider


class EvidenceIndex:
    def __init__(
        self,
        store: EvidenceStore,
        client: AsyncQdrantClient,
        embeddings: FastEmbedProvider,
        prefix: str,
    ) -> None:
        self.store, self.client = store, client
        self.embeddings = embeddings.cached_copy()
        self.collection = (
            prefix
            + "_project_evidence_"
            + hashlib.sha256(f"{embeddings.model}:{embeddings.dimensions}".encode()).hexdigest()[
                :12
            ]
        )
        self.retry_after = 0.0
        self.index_version = 1

    async def ensure(self) -> None:
        if await self.client.collection_exists(self.collection):
            return
        await self.client.create_collection(
            self.collection,
            vectors_config=models.VectorParams(
                size=self.embeddings.dimensions, distance=models.Distance.COSINE
            ),
        )
        await self.client.create_payload_index(
            self.collection, "project_id", models.PayloadSchemaType.KEYWORD
        )

    async def search(self, project_id: str, query: str, *, limit: int) -> dict[str, float]:
        if time.monotonic() < self.retry_after:
            return {}
        try:
            async with asyncio.timeout(8):
                if not await self.client.collection_exists(self.collection):
                    return {}
                await self.embeddings.require_cached_model()
                vector = await self.embeddings.embed_query(query)
                result = await self.client.query_points(
                    self.collection,
                    query=vector,
                    query_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="project_id", match=models.MatchValue(value=project_id)
                            ),
                            models.FieldCondition(
                                key="index_version",
                                match=models.MatchValue(value=self.index_version),
                            ),
                            models.FieldCondition(
                                key="model", match=models.MatchValue(value=self.embeddings.model)
                            ),
                        ]
                    ),
                    limit=limit,
                    with_payload=False,
                )
                return {str(point.id): float(point.score) for point in result.points}
        except Exception:
            self.retry_after = time.monotonic() + 60
            raise

    async def run_once(self) -> bool:
        try:
            async with asyncio.timeout(30):
                return await self._run_once()
        except TimeoutError:
            self.retry_after = time.monotonic() + 60
            return False

    async def _run_once(self) -> bool:
        if time.monotonic() < self.retry_after:
            return False
        async with self.store.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ProjectClaimRow)
                    .where(
                        (ProjectClaimRow.indexed_model.is_(None))
                        | (ProjectClaimRow.indexed_model != self.embeddings.model)
                        | (ProjectClaimRow.indexed_version == 0)
                    )
                    .order_by(ProjectClaimRow.id)
                    .limit(32)
                )
            )
            if not rows:
                return False
            try:
                # Never index or send text from another project or a withdrawn origin.
                valid = [
                    row
                    for row in rows
                    if await self.store.valid_claim(session, row, project_id=row.project_id)
                ]
                if valid:
                    await self.embeddings.require_cached_model()
                    vectors = await self.embeddings.embed_documents(
                        [row.text + "\n" + row.topic for row in valid]
                    )
                    await self.ensure()
                    await self.client.upsert(
                        self.collection,
                        points=[
                            models.PointStruct(
                                id=row.id,
                                vector=vector,
                                payload={
                                    "project_id": row.project_id,
                                    "model": self.embeddings.model,
                                    "index_version": self.index_version,
                                },
                            )
                            for row, vector in zip(valid, vectors, strict=True)
                        ],
                        wait=True,
                    )
                valid_ids = {row.id for row in valid}
                invalid = [row.id for row in rows if row.id not in valid_ids]
                if invalid and await self.client.collection_exists(self.collection):
                    await self.client.delete(
                        self.collection,
                        points_selector=models.PointIdsList.model_validate({"points": invalid}),
                        wait=True,
                    )
                for row in rows:
                    row.indexed_model = self.embeddings.model
                    row.indexed_version = self.index_version if row.id in valid_ids else -1
                await session.commit()
                return True
            except Exception:
                # PostgreSQL stays authoritative; retrieval can fall back to keywords.
                self.retry_after = time.monotonic() + 60
                return False
