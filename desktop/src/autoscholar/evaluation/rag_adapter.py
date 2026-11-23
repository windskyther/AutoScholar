"""RAG score adapters: fixture replay is explicitly NOT semantic retrieval evaluation."""

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from pydantic import Field, JsonValue, model_validator

from autoscholar.evaluation.datasets import decode_json, read_bounded
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    Identifier,
    MeasuredUsage,
    Observation,
    ScoreCard,
)
from autoscholar.evaluation.rag import BenchmarkRetriever
from autoscholar.evaluation.ranking import normalize_ks, score_ranking, validate_ids
from autoscholar.rag.models import RetrievalMode

MODES: tuple[RetrievalMode, ...] = ("dense", "sparse", "hybrid", "hybrid_rerank")
RelevanceID = Annotated[str, Field(min_length=1, max_length=200)]


class RAGInputs(EvaluationModel):
    query_id: Identifier


class RAGLabels(EvaluationModel):
    relevant_document_ids: list[RelevanceID] = Field(default_factory=list, max_length=100)
    relevant_chunk_ids: list[RelevanceID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def relevance(self) -> "RAGLabels":
        if not (self.relevant_document_ids or self.relevant_chunk_ids):
            raise ValueError("RAG benchmark needs relevant IDs")
        for ids in (self.relevant_document_ids, self.relevant_chunk_ids):
            validate_ids(ids)
            if len(ids) != len(set(ids)):
                raise ValueError("Relevance IDs must be unique")
        return self


class RankedChunk(EvaluationModel):
    id: RelevanceID
    document_id: RelevanceID


class FixtureChunk(RankedChunk):
    text: str = Field(min_length=1, max_length=8192)


class RAGObservation(EvaluationModel):
    ranking: list[RankedChunk] = Field(max_length=50)


class RankingFixture(EvaluationModel):
    schema_version: int = Field(ge=1, le=1)
    suite_id: Identifier
    description: str = Field(min_length=1, max_length=2000)
    chunks: list[FixtureChunk] = Field(min_length=1, max_length=1000)
    queries: dict[Identifier, str] = Field(min_length=1, max_length=1000)
    rankings: dict[
        Identifier, dict[RetrievalMode, Annotated[list[RelevanceID], Field(max_length=50)]]
    ] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bindings(self) -> "RankingFixture":
        ids = {chunk.id for chunk in self.chunks}
        if len(ids) != len(self.chunks) or self.queries.keys() != self.rankings.keys():
            raise ValueError("Fixture IDs/query bindings are invalid")
        for variants in self.rankings.values():
            if set(variants) != set(MODES):
                raise ValueError("Fixture must identify all four replay modes")
            for ranked in variants.values():
                validate_ids(ranked)
                if not set(ranked) <= ids:
                    raise ValueError("Fixture references unknown chunks")
        return self


def load_ranking_fixture(path: Path) -> tuple[RankingFixture, str]:
    import hashlib

    raw = read_bounded(path)
    return RankingFixture.model_validate(decode_json(raw)), hashlib.sha256(raw).hexdigest()


def score_observation(
    observation: Observation,
    expected: dict[str, JsonValue],
    ks: Sequence[int],
) -> ScoreCard:
    labels = RAGLabels.model_validate(expected)
    ranking = RAGObservation.model_validate(observation.payload).ranking
    cutoffs = normalize_ks(ks)
    metrics: dict[str, float | None] = {}
    checks: dict[str, bool] = {}
    for level, relevant, ranked in (
        ("document", labels.relevant_document_ids, [item.document_id for item in ranking]),
        ("chunk", labels.relevant_chunk_ids, [item.id for item in ranking]),
    ):
        if not relevant:
            continue
        result = score_ranking(ranked, relevant, ks=cutoffs)
        metrics[f"{level}_mrr"] = result.mrr
        for k in cutoffs:
            metrics[f"{level}_recall_at_{k}"] = result.recall_at_k[k]
            metrics[f"{level}_hit_rate_at_{k}"] = result.hit_rate_at_k[k]
            metrics[f"{level}_ndcg_at_{k}"] = result.ndcg_at_k[k]
        checks[f"{level}_hit"] = result.hit_rate_at_k[cutoffs[-1]] == 1.0
    return ScoreCard(checks=checks, metrics=metrics)


class ReplayRAGAdapter:
    def __init__(
        self,
        fixture: RankingFixture,
        *,
        fixture_sha256: str,
        mode: RetrievalMode,
        ks: Sequence[int] = (5, 10),
    ) -> None:
        if mode not in MODES:
            raise ValueError("Unknown retrieval mode")
        self.fixture = fixture
        self.mode = mode
        self.ks = normalize_ks(ks)
        self.chunks = {chunk.id: chunk for chunk in fixture.chunks}
        self.identity = AdapterIdentity(
            name="rag-ranking-replay",
            category="rag",
            variant=mode,
            execution="fixture",
            resources={"ranking_fixture": fixture_sha256},
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = RAGInputs.model_validate(case.inputs)
        labels = RAGLabels.model_validate(case.expected)
        if self.fixture.queries.get(query.query_id) != case.prompt:
            raise ValueError("Fixture query does not match benchmark input")
        if not set(labels.relevant_chunk_ids) <= self.chunks.keys():
            raise ValueError("Unknown relevant chunk")
        if not set(labels.relevant_document_ids) <= {
            chunk.document_id for chunk in self.fixture.chunks
        }:
            raise ValueError("Unknown relevant document")
        if labels.relevant_document_ids and not {
            self.chunks[identifier].document_id for identifier in labels.relevant_chunk_ids
        } <= set(labels.relevant_document_ids):
            raise ValueError("Document and chunk labels disagree")

    async def execute(
        self,
        prompt: str,
        inputs: dict[str, JsonValue],
        *,
        seed: int,
    ) -> Observation:
        query = RAGInputs.model_validate(inputs)
        if self.fixture.queries.get(query.query_id) != prompt:
            raise ValueError("Query changed after validation")
        ranked = self.fixture.rankings[query.query_id][self.mode][: self.ks[-1]]
        payload = RAGObservation(
            ranking=[
                RankedChunk(id=identifier, document_id=self.chunks[identifier].document_id)
                for identifier in ranked
            ]
        ).model_dump(mode="json")
        return Observation(
            payload=payload,
            usage=MeasuredUsage(
                model_calls=0,
                external_api_calls=0,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
            ),
        )

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_observation(observation, expected, self.ks)


class InjectedRAGAdapter:
    """For an explicitly provisioned retriever; no implicit provider/Settings factory."""

    def __init__(
        self,
        retriever: BenchmarkRetriever,
        *,
        project_id: str,
        identity: AdapterIdentity,
        mode: RetrievalMode,
        ks: Sequence[int] = (5, 10),
    ) -> None:
        if identity.category != "rag" or identity.execution != "injected" or mode not in MODES:
            raise ValueError("Injected RAG identity/mode is invalid")
        self.retriever, self.project_id = retriever, project_id
        self.identity, self.mode, self.ks = identity, mode, normalize_ks(ks)

    def validate_case(self, case: BenchmarkCase) -> None:
        RAGInputs.model_validate(case.inputs)
        RAGLabels.model_validate(case.expected)

    async def execute(
        self,
        prompt: str,
        inputs: dict[str, JsonValue],
        *,
        seed: int,
    ) -> Observation:
        chunks = await self.retriever.retrieve(
            prompt,
            project_id=self.project_id,
            retrieval_mode=self.mode,
            top_k=self.ks[-1],
        )
        if any(chunk.project_id != self.project_id for chunk in chunks):
            raise ValueError("Retriever returned chunks from another project")
        payload = RAGObservation(
            ranking=[
                RankedChunk(id=chunk.id, document_id=chunk.document_id)
                for chunk in chunks[: self.ks[-1]]
            ]
        ).model_dump(mode="json")
        # Embedding/provider usage cannot be inferred from a list of retrieved chunks.
        return Observation(payload=payload)

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_observation(observation, expected, self.ks)
