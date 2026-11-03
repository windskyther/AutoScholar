from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from autoscholar.evaluation.datasets import decode_json, read_bounded
from autoscholar.evaluation.ranking import (
    RankingAggregate,
    RankingScore,
    aggregate_rankings,
    normalize_ks,
    score_ranking,
    validate_ids,
)
from autoscholar.rag.models import RetrievalMode, RetrievedChunk

if TYPE_CHECKING:
    from autoscholar.core.config import Settings


@dataclass(frozen=True, slots=True)
class RAGBenchmarkItem:
    question: str
    relevant_document_ids: tuple[str, ...] = ()
    relevant_chunk_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("Benchmark needs a question")
        if not (self.relevant_document_ids or self.relevant_chunk_ids):
            raise ValueError("Benchmark needs relevant IDs")
        validate_ids(self.relevant_document_ids)
        validate_ids(self.relevant_chunk_ids)


@dataclass(frozen=True, slots=True)
class RAGBenchmarkResult:
    mode: RetrievalMode
    count: int
    recall_at_k: dict[int, float]
    hit_rate_at_k: dict[int, float]
    mrr: float
    ndcg_at_k: dict[int, float]
    document_metrics: RankingAggregate | None = None
    chunk_metrics: RankingAggregate | None = None


class BenchmarkRetriever(Protocol):
    async def retrieve(
        self,
        question: str,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        retrieval_mode: RetrievalMode = "hybrid_rerank",
        top_k: int | None = None,
    ) -> list[RetrievedChunk]: ...


def load_rag_benchmark(path: Path) -> list[RAGBenchmarkItem]:
    items: list[RAGBenchmarkItem] = []
    for line_number, raw_line in enumerate(
        read_bounded(path).decode("utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = decode_json(line.encode("utf-8"))
            if not isinstance(payload, dict) or set(payload) - {
                "question",
                "relevant_document_ids",
                "relevant_document_id",
                "relevant_chunk_ids",
                "relevant_chunk_id",
            }:
                raise ValueError("Unsupported benchmark fields")
        except ValueError as exc:
            raise ValueError(f"Invalid JSON on benchmark line {line_number}") from exc
        question = payload.get("question")
        if not isinstance(question, str):
            raise ValueError(f"Benchmark line {line_number} needs a string question")
        question = question.strip()
        document_ids = _ids(payload, "relevant_document_ids", "relevant_document_id")
        chunk_ids = _ids(payload, "relevant_chunk_ids", "relevant_chunk_id")
        if not question or not (document_ids or chunk_ids):
            raise ValueError(f"Benchmark line {line_number} needs a question and relevant IDs")
        items.append(
            RAGBenchmarkItem(
                question=question,
                relevant_document_ids=document_ids,
                relevant_chunk_ids=chunk_ids,
            )
        )
    if not items:
        raise ValueError("RAG benchmark dataset is empty")
    return items


async def evaluate_rag(
    retriever: BenchmarkRetriever,
    *,
    project_id: str,
    items: Sequence[RAGBenchmarkItem],
    mode: RetrievalMode,
    ks: Sequence[int] = (5, 10),
) -> RAGBenchmarkResult:
    if not items:
        raise ValueError("RAG benchmark dataset is empty")
    normalized_ks = normalize_ks(ks)
    max_k = normalized_ks[-1]
    primary: list[RankingScore] = []
    documents: list[RankingScore] = []
    chunks: list[RankingScore] = []
    for item in items:
        retrieved = await retriever.retrieve(
            item.question,
            project_id=project_id,
            retrieval_mode=mode,
            top_k=max_k,
        )
        if any(chunk.project_id != project_id for chunk in retrieved):
            raise ValueError("Retriever returned chunks from another project")
        if item.relevant_document_ids:
            documents.append(
                score_ranking(
                    [chunk.document_id for chunk in retrieved],
                    item.relevant_document_ids,
                    ks=normalized_ks,
                )
            )
        if item.relevant_chunk_ids:
            chunks.append(
                score_ranking(
                    [chunk.id for chunk in retrieved], item.relevant_chunk_ids, ks=normalized_ks
                )
            )
        primary.append(chunks[-1] if item.relevant_chunk_ids else documents[-1])
    aggregate = aggregate_rankings(primary)
    assert aggregate is not None
    return RAGBenchmarkResult(
        mode=mode,
        count=len(items),
        recall_at_k=aggregate.recall_at_k,
        hit_rate_at_k=aggregate.hit_rate_at_k,
        mrr=aggregate.mrr,
        ndcg_at_k=aggregate.ndcg_at_k,
        document_metrics=aggregate_rankings(documents),
        chunk_metrics=aggregate_rankings(chunks),
    )


def _ids(payload: dict[str, object], plural: str, singular: str) -> tuple[str, ...]:
    raw_plural = payload.get(plural)
    raw_singular = payload.get(singular)
    if raw_plural is not None and not isinstance(raw_plural, list):
        raise ValueError("Relevance IDs must be a list")
    if raw_singular is not None and not isinstance(raw_singular, str):
        raise ValueError("Relevance ID must be a string")
    values = (
        raw_plural
        if raw_plural is not None
        else ([raw_singular] if raw_singular is not None else [])
    )
    validate_ids(values)
    if raw_plural is not None and raw_singular is not None and values != [raw_singular]:
        raise ValueError("Singular/plural relevance labels disagree")
    return tuple(dict.fromkeys(values))


async def _run_cli(args: argparse.Namespace, settings: Settings) -> None:
    from autoscholar.agent.repository import AgentTaskRepository
    from autoscholar.infrastructure import Database, Qdrant
    from autoscholar.llm import create_llm_provider
    from autoscholar.rag import (
        FastEmbedReranker,
        FastEmbedSparseProvider,
        KnowledgeRepository,
        QdrantChunkIndex,
        RAGQueryService,
    )
    from autoscholar.rag.worker import create_embedding_provider

    database = Database(settings.database_url)
    qdrant_key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    qdrant = Qdrant(settings.qdrant_url, api_key=qdrant_key)
    dense = create_embedding_provider(settings)
    sparse = FastEmbedSparseProvider(
        model=settings.sparse_model,
        cache_dir=settings.model_cache_path,
    )
    reranker = FastEmbedReranker(
        model=settings.reranker_model,
        cache_dir=settings.model_cache_path,
    )
    llm = create_llm_provider(settings)
    sessions = database.session_factory
    retriever = RAGQueryService(
        provider=llm,
        embeddings=dense,
        sparse_embeddings=sparse,
        reranker=reranker,
        index=QdrantChunkIndex(
            qdrant.client,
            collection=settings.qdrant_collection,
            dense_dimensions=dense.dimensions,
        ),
        knowledge=KnowledgeRepository(sessions),
        tasks=AgentTaskRepository(sessions),
        default_top_k=max(args.ks),
        candidate_limit=settings.rag_candidate_limit,
    )
    try:
        items = load_rag_benchmark(args.dataset)
        results = [
            await evaluate_rag(
                retriever,
                project_id=args.project_id,
                items=items,
                mode=mode,
                ks=args.ks,
            )
            for mode in args.modes
        ]
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    finally:
        await llm.close()
        await reranker.close()
        await sparse.close()
        await dense.close()
        await qdrant.close()
        await database.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate AutoScholar RAG retrieval")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["dense", "sparse", "hybrid", "hybrid_rerank"],
        default=["dense", "sparse", "hybrid", "hybrid_rerank"],
    )
    parser.add_argument("--ks", nargs="+", type=int, default=[5, 10])
    return parser


def main() -> None:
    from autoscholar.core.config import get_settings

    args = _parser().parse_args()
    asyncio.run(_run_cli(args, get_settings()))


if __name__ == "__main__":
    main()
