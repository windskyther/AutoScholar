"""Real local retrieval with controlled lexical algorithms, not ranking replay or neural RAG."""

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, JsonValue, model_validator
from qdrant_client import AsyncQdrantClient

from autoscholar.evaluation.component_fixture import VersionedFixture, fixture_usage
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    Observation,
    ScoreCard,
)
from autoscholar.evaluation.rag_adapter import (
    FixtureChunk,
    RAGInputs,
    RAGLabels,
    RankedChunk,
    score_observation,
)
from autoscholar.evaluation.ranking import normalize_ks
from autoscholar.evaluation.workflow_runtime import local_workflow
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from autoscholar.rag.database_models import DocumentRow
from autoscholar.rag.embeddings import SparseVectorData
from autoscholar.rag.index import QdrantChunkIndex
from autoscholar.rag.models import DocumentChunkRecord, RetrievedChunk
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.service import RAGQueryService

RetrievalVariant = Literal["baseline", "no_reranker"]
RETRIEVAL_VARIANTS: tuple[RetrievalVariant, ...] = ("baseline", "no_reranker")


class RetrievalFixture(VersionedFixture):
    chunks: list[FixtureChunk] = Field(min_length=1, max_length=64)
    queries: dict[str, str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bindings(self) -> "RetrievalFixture":
        if len({item.id for item in self.chunks}) != len(self.chunks):
            raise ValueError("Duplicate retrieval chunk")
        if any(not query.strip() or len(query) > 10000 for query in self.queries.values()):
            raise ValueError("Invalid bounded retrieval query")
        return self


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class Vocabulary:
    def __init__(self, fixture: RetrievalFixture) -> None:
        words = sorted(
            {
                word
                for text in [*(c.text for c in fixture.chunks), *fixture.queries.values()]
                for word in tokens(text)
            }
        )
        if not words or len(words) > 1024:
            raise ValueError("Controlled lexical vocabulary is empty or too large")
        self.words = {word: index for index, word in enumerate(words)}

    def counts(self, text: str) -> dict[int, float]:
        counts = Counter(word for word in tokens(text) if word in self.words)
        if not counts:
            raise ValueError("Controlled query has no vocabulary terms")
        return {self.words[word]: float(count) for word, count in counts.items()}


class LexicalDense:
    model = "public-bag-of-words-counts-v1"

    def __init__(self, vocabulary: Vocabulary) -> None:
        self.vocabulary = vocabulary
        self.dimensions = len(vocabulary.words)
        self.calls = 0

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        result = []
        for document in documents:
            vector = [0.0] * self.dimensions
            for index, count in self.vocabulary.counts(document).items():
                vector[index] = count
            result.append(vector)
        return result

    async def embed_query(self, query: str) -> list[float]:
        self.calls += 1
        return (await self.embed_documents([query]))[0]

    async def close(self) -> None:
        pass


class LexicalSparse:
    model = "public-sparse-token-counts-v1"

    def __init__(self, vocabulary: Vocabulary) -> None:
        self.vocabulary = vocabulary
        self.calls = 0

    async def embed_documents(self, documents: Sequence[str]) -> list[SparseVectorData]:
        result = []
        for document in documents:
            counts = self.vocabulary.counts(document)
            indices = sorted(counts)
            result.append(SparseVectorData(indices, [counts[index] for index in indices]))
        return result

    async def embed_query(self, query: str) -> SparseVectorData:
        self.calls += 1
        return (await self.embed_documents([query]))[0]

    async def close(self) -> None:
        pass


class LexicalReranker:
    model = "public-jaccard-token-overlap-v1"
    calls = 0

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        self.calls += 1
        query_terms = set(tokens(query))

        def score(chunk: RetrievedChunk) -> float:
            terms = set(tokens(chunk.content))
            return len(query_terms & terms) / max(1, len(query_terms | terms))

        # Stable ties preserve the actual RRF ranking. No relevance IDs or fixed order.
        return [
            replace(chunk, score=score(chunk))
            for chunk in sorted(chunks, key=score, reverse=True)[:limit]
        ]

    async def close(self) -> None:
        pass


class NoGeneration:
    configured = False

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        raise ValueError("Retrieval ablation never generates answers or calls a provider")

    async def close(self) -> None:
        pass


class ObservedIndex(QdrantChunkIndex):
    candidates: list[RetrievedChunk]
    searches = 0
    candidate_limit = 0

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
        self.candidate_limit = limit
        self.candidates = await super().search_hybrid(
            project_id=project_id,
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            document_ids=document_ids,
            limit=limit,
        )
        return self.candidates


class RetrievalObservation(EvaluationModel):
    ranking: list[RankedChunk] = Field(max_length=50)
    candidates: list[str] = Field(max_length=50)
    candidate_limit: int = Field(ge=1, le=50)
    dense_calls: int = Field(ge=0, le=1)
    sparse_calls: int = Field(ge=0, le=1)
    index_searches: int = Field(ge=0, le=1)
    reranker_calls: int = Field(ge=0, le=1)
    scope_bound: bool
    paired_candidates: bool | None = None


class RetrievalPairAudit:
    """Baseline-first audit of actual ordered candidate pools, never a source of rankings."""

    def __init__(self) -> None:
        self.baselines: dict[tuple[str, int], str] = {}

    def observe(
        self, variant: RetrievalVariant, query: str, seed: int, candidates: list[str]
    ) -> bool | None:
        key = query, seed
        digest = payload_digest(candidates)
        if variant == "baseline":
            self.baselines[key] = digest
            return None
        return self.baselines.get(key) == digest


class InjectedRetrievalAblationAdapter:
    def __init__(
        self,
        fixture: RetrievalFixture,
        *,
        fixture_sha256: str,
        variant: RetrievalVariant,
        workspace_root: Path,
        audit: RetrievalPairAudit,
        ks: Sequence[int] = (1, 5, 10),
    ) -> None:
        if variant not in ("baseline", "no_reranker"):
            raise ValueError("Unknown retrieval ablation")
        self.fixture, self.variant, self.workspace_root, self.audit = (
            fixture,
            variant,
            workspace_root,
            audit,
        )
        self.ks = normalize_ks(ks)
        self.vocabulary = Vocabulary(fixture)
        self.identity = AdapterIdentity(
            name="local-lexical-retrieval-ablation",
            category="rag",
            variant=variant,
            execution="injected",
            model=f"Qdrant Local {version('qdrant-client')}; "
            "token-count cosine/sparse/RRF; Jaccard reranker v1",
            resources={
                "retrieval_fixture": fixture_sha256,
                "retrieval_algorithm": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            },
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = RAGInputs.model_validate(case.inputs)
        labels = RAGLabels.model_validate(case.expected)
        if self.fixture.queries.get(query.query_id) != case.prompt:
            raise ValueError("Retrieval query binding differs")
        if not set(labels.relevant_chunk_ids) <= {
            item.id for item in self.fixture.chunks
        } or not set(labels.relevant_document_ids) <= {
            item.document_id for item in self.fixture.chunks
        }:
            raise ValueError("Unknown retrieval relevance IDs")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = RAGInputs.model_validate(inputs)
        if self.fixture.queries.get(query.query_id) != prompt:
            raise ValueError("Retrieval query changed after preflight")
        dense, sparse, reranker = (
            LexicalDense(self.vocabulary),
            LexicalSparse(self.vocabulary),
            LexicalReranker(),
        )
        client = AsyncQdrantClient(location=":memory:")
        try:
            async with local_workflow(self.workspace_root) as state:
                knowledge = KnowledgeRepository(state.tasks.session_factory)
                project = await knowledge.create_project(
                    name="Public lexical retrieval", description="Isolated evaluation"
                )
                index = ObservedIndex(
                    client, collection="public-ablation", dense_dimensions=dense.dimensions
                )
                chunks_by_uuid = {}
                documents_by_uuid = {}
                for public_id in sorted({item.document_id for item in self.fixture.chunks}):
                    entries = [
                        item for item in self.fixture.chunks if item.document_id == public_id
                    ]
                    document_id = str(
                        uuid5(NAMESPACE_URL, "autoscholar-eval-document/" + public_id)
                    )
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
                    ready = replace(
                        document,
                        status="ready",
                        chunk_count=len(entries),
                        embedding_model=dense.model,
                        index_version=2,
                    )
                    chunks = []
                    for ordinal, item in enumerate(entries):
                        chunk_id = str(uuid5(NAMESPACE_URL, "autoscholar-eval-chunk/" + item.id))
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
                                token_count=len(tokens(item.text)),
                                created_at=datetime.now(UTC),
                            )
                        )
                    documents_by_uuid[document_id] = public_id
                    await index.replace_document(
                        ready,
                        chunks,
                        await dense.embed_documents([item.text for item in entries]),
                        await sparse.embed_documents([item.text for item in entries]),
                    )
                service = RAGQueryService(
                    provider=NoGeneration(),
                    embeddings=dense,
                    sparse_embeddings=sparse,
                    reranker=reranker,
                    index=index,
                    knowledge=knowledge,
                    tasks=state.tasks,
                    default_top_k=self.ks[-1],
                    candidate_limit=self.ks[-1],
                )
                ranked = await service.retrieve(
                    prompt,
                    project_id=project.id,
                    retrieval_mode="hybrid_rerank" if self.variant == "baseline" else "hybrid",
                    top_k=self.ks[-1],
                )
                candidates = [chunks_by_uuid[item.id] for item in index.candidates]
                actual = RetrievalObservation(
                    ranking=[
                        RankedChunk(
                            id=chunks_by_uuid[item.id],
                            document_id=documents_by_uuid[item.document_id],
                        )
                        for item in ranked
                    ],
                    candidates=candidates,
                    candidate_limit=index.candidate_limit,
                    dense_calls=dense.calls,
                    sparse_calls=sparse.calls,
                    index_searches=index.searches,
                    reranker_calls=reranker.calls,
                    scope_bound=all(
                        item.project_id == project.id
                        and item.id in {c.id for c in index.candidates}
                        for item in ranked
                    ),
                    paired_candidates=self.audit.observe(
                        self.variant, query.query_id, seed, candidates
                    ),
                )
                return Observation(
                    payload=actual.model_dump(mode="json"), usage=fixture_usage(model_calls=0)
                )
        finally:
            await client.close()

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        actual = RetrievalObservation.model_validate(observation.payload)
        ranking = Observation(
            payload={"ranking": [item.model_dump(mode="json") for item in actual.ranking]}
        )
        score = score_observation(ranking, expected, self.ks)
        # Relevance is a measured outcome, NOT an engineering acceptance prerequisite.
        score.checks = {
            "actual_retrieval_path": actual.dense_calls
            == actual.sparse_calls
            == actual.index_searches
            == 1,
            "reranker_path": actual.reranker_calls == int(self.variant == "baseline"),
            "candidate_scope": actual.scope_bound
            and len(actual.candidates) == len(set(actual.candidates)),
            "matched_candidate_limit": actual.candidate_limit == self.ks[-1],
            "paired_candidate_pool": actual.paired_candidates is True
            if self.variant == "no_reranker"
            else actual.paired_candidates is None,
            "unmodified_without_reranker": self.variant == "baseline"
            or [item.id for item in actual.ranking] == actual.candidates[: self.ks[-1]],
        }
        score.metrics.update(
            {
                "reranker_calls": float(actual.reranker_calls),
                "retrieved_candidates": float(len(actual.candidates)),
                "dense_query_calls": float(actual.dense_calls),
                "sparse_query_calls": float(actual.sparse_calls),
            }
        )
        return score
