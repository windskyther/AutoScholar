"""Actual default-model inference + scoped Qdrant retrieval; gold is scoring-only."""

import hashlib
import math
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, JsonValue, model_validator
from qdrant_client import AsyncQdrantClient

from autoscholar.evaluation.component_fixture import fixture_usage
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    FiniteNumber,
    Observation,
    ScoreCard,
)
from autoscholar.evaluation.neural_client import DockerNeuralClient
from autoscholar.evaluation.neural_worker import DENSE_MODEL, RERANKER_MODEL, SPARSE_MODEL
from autoscholar.evaluation.rag_adapter import (
    MODES,
    RAGInputs,
    RAGLabels,
    RankedChunk,
    score_observation,
)
from autoscholar.evaluation.retrieval_ablation import NoGeneration, RetrievalFixture
from autoscholar.evaluation.workflow_runtime import local_workflow
from autoscholar.rag.database_models import DocumentRow
from autoscholar.rag.embeddings import SparseVectorData
from autoscholar.rag.index import QdrantChunkIndex
from autoscholar.rag.models import DocumentChunkRecord, RetrievalMode, RetrievedChunk
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.service import RAGQueryService

KS = (1, 3, 5)
CANDIDATE_LIMIT = 10


class DenseVectors(EvaluationModel):
    vectors: list[Annotated[list[FiniteNumber], Field(min_length=384, max_length=384)]] = Field(
        min_length=1, max_length=64
    )


class SparseVector(EvaluationModel):
    indices: list[Annotated[int, Field(ge=0, le=2**32 - 1)]] = Field(max_length=10000)
    values: list[FiniteNumber] = Field(max_length=10000)

    @model_validator(mode="after")
    def consistent(self) -> "SparseVector":
        if len(self.indices) != len(self.values) or len(set(self.indices)) != len(self.indices):
            raise ValueError("Invalid sparse inference dimensions")
        return self


class RankedScore(EvaluationModel):
    id: str = Field(min_length=1, max_length=200)
    score: FiniteNumber


class RemoteDense:
    model = DENSE_MODEL
    dimensions = 384

    def __init__(self, client: DockerNeuralClient) -> None:
        self.client = client

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        raw = await self.client.call("dense_documents", {"texts": list(documents)})
        vectors = DenseVectors.model_validate({"vectors": raw}).vectors
        if len(vectors) != len(documents):
            raise ValueError("Dense document inference count mismatch")
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        raw = await self.client.call("dense_query", {"query": query})
        return DenseVectors.model_validate({"vectors": [raw]}).vectors[0]

    async def close(self) -> None:
        pass


class RemoteSparse:
    model = SPARSE_MODEL

    def __init__(self, client: DockerNeuralClient) -> None:
        self.client = client

    async def embed_documents(self, documents: Sequence[str]) -> list[SparseVectorData]:
        raw = await self.client.call("sparse_documents", {"texts": list(documents)})
        if not isinstance(raw, list) or len(raw) != len(documents):
            raise ValueError("Sparse document inference count mismatch")
        result = [SparseVector.model_validate(item) for item in raw]
        return [SparseVectorData(item.indices, item.values) for item in result]

    async def embed_query(self, query: str) -> SparseVectorData:
        raw = await self.client.call("sparse_query", {"query": query})
        result = SparseVector.model_validate(raw)
        return SparseVectorData(result.indices, result.values)

    async def close(self) -> None:
        pass


class RemoteReranker:
    model = RERANKER_MODEL

    def __init__(self, client: DockerNeuralClient) -> None:
        self.client = client

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        raw = await self.client.call(
            "rerank",
            {"query": query, "chunks": [asdict(chunk) for chunk in chunks], "limit": limit},
        )
        if not isinstance(raw, list) or len(raw) != min(limit, len(chunks)):
            raise ValueError("Neural reranker returned incomplete ranking")
        scores = [RankedScore.model_validate(item) for item in raw]
        by_id = {chunk.id: chunk for chunk in chunks}
        if len({item.id for item in scores}) != len(scores) or any(
            item.id not in by_id for item in scores
        ):
            raise ValueError("Neural reranker invented or repeated a candidate")
        if any(left.score < right.score for left, right in pairwise(scores)):
            raise ValueError("Neural reranker score order is inconsistent")
        return [replace(by_id[item.id], score=item.score) for item in scores]

    async def close(self) -> None:
        pass


class NeuralObservedIndex(QdrantChunkIndex):
    def __init__(self, client: AsyncQdrantClient) -> None:
        super().__init__(client, collection="public-neural-semantic", dense_dimensions=384)
        self.searches = 0
        self.candidates: list[RetrievedChunk] = []
        self.limit = 0

    async def search_dense(
        self,
        *,
        project_id: str,
        vector: Sequence[float],
        document_ids: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[RetrievedChunk]:
        self.searches += 1
        self.limit = limit
        self.candidates = await super().search_dense(
            project_id=project_id, vector=vector, document_ids=document_ids, limit=limit
        )
        return self.candidates

    async def search_sparse(
        self,
        *,
        project_id: str,
        vector: SparseVectorData,
        document_ids: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[RetrievedChunk]:
        self.searches += 1
        self.limit = limit
        self.candidates = await super().search_sparse(
            project_id=project_id, vector=vector, document_ids=document_ids, limit=limit
        )
        return self.candidates

    async def search_hybrid(
        self,
        *,
        project_id: str,
        dense_vector: Sequence[float],
        sparse_vector: SparseVectorData,
        document_ids: Sequence[str] | None = None,
        limit: int = 30,
    ) -> list[RetrievedChunk]:
        self.searches += 1
        self.limit = limit
        self.candidates = await super().search_hybrid(
            project_id=project_id,
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            document_ids=document_ids,
            limit=limit,
        )
        return self.candidates


class NeuralObservation(EvaluationModel):
    ranking: list[RankedChunk] = Field(max_length=5)
    candidates: list[str] = Field(max_length=10)
    dense_calls: int = Field(ge=0)
    sparse_calls: int = Field(ge=0)
    reranker_calls: int = Field(ge=0)
    index_searches: int = Field(ge=0)
    scope_bound: bool
    paired_candidates: bool | None
    candidate_limit: int


class NeuralRetrievalAdapter:
    def __init__(
        self,
        fixture: RetrievalFixture,
        *,
        fixture_sha256: str,
        mode: RetrievalMode,
        service: RAGQueryService,
        index: NeuralObservedIndex,
        client: DockerNeuralClient,
        project_id: str,
        chunks: dict[str, str],
        documents: dict[str, str],
        pair_audit: dict[tuple[str, int], str],
    ) -> None:
        if mode not in MODES or client.identity is None or client.image_sha256 is None:
            raise ValueError("Actual preflighted neural models are required")
        self.fixture, self.mode, self.service, self.index, self.client = (
            fixture,
            mode,
            service,
            index,
            client,
        )
        self.project_id, self.chunks, self.documents, self.pair_audit = (
            project_id,
            chunks,
            documents,
            pair_audit,
        )
        self.identity = AdapterIdentity(
            name="actual-cached-neural-retrieval",
            category="rag",
            variant=mode,
            execution="injected",
            model="; ".join(client.identity.models),
            resources={
                **client.identity.resources,
                "inference_image": client.image_sha256,
                "retrieval_fixture": fixture_sha256,
                "retrieval_code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            },
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = RAGInputs.model_validate(case.inputs)
        labels = RAGLabels.model_validate(case.expected)
        if self.fixture.queries.get(query.query_id) != case.prompt:
            raise ValueError("Neural query differs from its independent input fixture")
        by_id = {item.id: item.document_id for item in self.fixture.chunks}
        if not set(labels.relevant_chunk_ids) <= by_id.keys() or not set(
            labels.relevant_document_ids
        ) <= set(by_id.values()):
            raise ValueError("Neural labels refer to unknown corpus entries")
        if labels.relevant_document_ids and not {
            by_id[key] for key in labels.relevant_chunk_ids
        } <= set(labels.relevant_document_ids):
            raise ValueError("Neural document/chunk labels disagree")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = RAGInputs.model_validate(inputs)
        if self.fixture.queries.get(query.query_id) != prompt:
            raise ValueError("Neural query changed after preflight")
        before = dict(self.client.counts)
        searches = self.index.searches
        # Use exactly the same ten-candidate hybrid pool in both compared variants.
        ranked = await self.service.retrieve(
            prompt,
            project_id=self.project_id,
            retrieval_mode=self.mode,
            top_k=CANDIDATE_LIMIT if self.mode == "hybrid" else KS[-1],
        )
        candidates = [self.chunks[item.id] for item in self.index.candidates]
        key = query.query_id, seed
        paired = None
        if self.mode == "hybrid":
            self.pair_audit[key] = payload_digest(candidates)
        elif self.mode == "hybrid_rerank":
            paired = self.pair_audit.get(key) == payload_digest(candidates)
        counts = self.client.counts
        payload = NeuralObservation(
            ranking=[
                RankedChunk(id=self.chunks[item.id], document_id=self.documents[item.document_id])
                for item in ranked[: KS[-1]]
            ],
            candidates=candidates,
            dense_calls=counts.get("dense_query", 0) - before.get("dense_query", 0),
            sparse_calls=counts.get("sparse_query", 0) - before.get("sparse_query", 0),
            reranker_calls=counts.get("rerank", 0) - before.get("rerank", 0),
            index_searches=self.index.searches - searches,
            scope_bound=all(
                item.project_id == self.project_id
                and item.id in {c.id for c in self.index.candidates}
                and math.isfinite(item.score)
                for item in ranked
            ),
            paired_candidates=paired,
            candidate_limit=self.index.limit,
        )
        return Observation(
            payload=payload.model_dump(mode="json"), usage=fixture_usage(model_calls=0)
        )

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        actual = NeuralObservation.model_validate(observation.payload)
        score = score_observation(
            Observation(
                payload={"ranking": [item.model_dump(mode="json") for item in actual.ranking]}
            ),
            expected,
            KS,
        )
        score.checks = {
            "actual_neural_path": actual.dense_calls == int(self.mode != "sparse")
            and actual.sparse_calls == int(self.mode != "dense")
            and actual.reranker_calls == int(self.mode == "hybrid_rerank")
            and actual.index_searches == 1,
            "project_candidate_scope": actual.scope_bound,
            "unique_candidates": len(actual.candidates) == len(set(actual.candidates)),
            "paired_hybrid_pool": actual.paired_candidates is True
            if self.mode == "hybrid_rerank"
            else actual.paired_candidates is None,
            "candidate_limit": actual.candidate_limit
            == (CANDIDATE_LIMIT if self.mode.startswith("hybrid") else KS[-1]),
        }
        score.metrics.update(
            {
                "dense_query_calls": float(actual.dense_calls),
                "sparse_query_calls": float(actual.sparse_calls),
                "reranker_calls": float(actual.reranker_calls),
                "candidate_count": float(len(actual.candidates)),
            }
        )
        return score


@asynccontextmanager
async def neural_adapters(
    fixture: RetrievalFixture,
    *,
    fixture_sha256: str,
    client: DockerNeuralClient,
    workspace_root: Path,
) -> AsyncIterator[list[NeuralRetrievalAdapter]]:
    qdrant = AsyncQdrantClient(location=":memory:")
    dense, sparse, reranker = RemoteDense(client), RemoteSparse(client), RemoteReranker(client)
    try:
        async with local_workflow(workspace_root) as state:
            knowledge = KnowledgeRepository(state.tasks.session_factory)
            project = await knowledge.create_project(
                name="Public neural semantic evaluation", description="Isolated public corpus"
            )
            index = NeuralObservedIndex(qdrant)
            chunks_by_uuid, documents_by_uuid = {}, {}
            # Index public text only. Gold never enters embeddings, Qdrant or inference worker.
            texts = [item.text for item in fixture.chunks]
            dense_vectors, sparse_vectors = (
                await dense.embed_documents(texts),
                await sparse.embed_documents(texts),
            )
            positions = {item.id: position for position, item in enumerate(fixture.chunks)}
            for public_id in sorted({item.document_id for item in fixture.chunks}):
                entries = [item for item in fixture.chunks if item.document_id == public_id]
                document_id = str(uuid5(NAMESPACE_URL, "public-neural-doc/" + public_id))
                document = await knowledge.create_document(
                    document_id=document_id,
                    project_id=project.id,
                    original_filename=public_id + ".txt",
                    title=public_id,
                    content_type="text/plain",
                    storage_key=document_id,
                    sha256=payload_digest([item.text for item in entries]),
                    size_bytes=sum(len(item.text.encode()) for item in entries),
                )
                async with state.tasks.session_factory() as session:
                    row = await session.get(DocumentRow, document_id)
                    assert row is not None
                    row.status, row.chunk_count, row.embedding_model, row.index_version = (
                        "ready",
                        len(entries),
                        dense.model,
                        2,
                    )
                    await session.commit()
                chunks = []
                for ordinal, item in enumerate(entries):
                    chunk_id = str(uuid5(NAMESPACE_URL, "public-neural-chunk/" + item.id))
                    chunks_by_uuid[chunk_id] = item.id
                    chunks.append(
                        DocumentChunkRecord(
                            id=chunk_id,
                            document_id=document_id,
                            project_id=project.id,
                            ordinal=ordinal,
                            page=1,
                            section=None,
                            content=item.text,
                            token_count=max(1, len(item.text.split())),
                            created_at=datetime.now(UTC),
                        )
                    )
                documents_by_uuid[document_id] = public_id
                await index.replace_document(
                    replace(
                        document,
                        status="ready",
                        chunk_count=len(entries),
                        embedding_model=dense.model,
                        index_version=2,
                    ),
                    chunks,
                    [dense_vectors[positions[item.id]] for item in entries],
                    [sparse_vectors[positions[item.id]] for item in entries],
                )
            service = RAGQueryService(
                provider=NoGeneration(),
                embeddings=dense,
                sparse_embeddings=sparse,
                reranker=reranker,
                index=index,
                knowledge=knowledge,
                tasks=state.tasks,
                default_top_k=KS[-1],
                candidate_limit=CANDIDATE_LIMIT,
            )
            audit: dict[tuple[str, int], str] = {}
            yield [
                NeuralRetrievalAdapter(
                    fixture,
                    fixture_sha256=fixture_sha256,
                    mode=mode,
                    service=service,
                    index=index,
                    client=client,
                    project_id=project.id,
                    chunks=chunks_by_uuid,
                    documents=documents_by_uuid,
                    pair_audit=audit,
                )
                for mode in MODES
            ]
    finally:
        await qdrant.close()
