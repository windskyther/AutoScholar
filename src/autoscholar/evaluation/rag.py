import argparse
import asyncio
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.core.config import Settings, get_settings
from autoscholar.infrastructure import Database, Qdrant
from autoscholar.llm import create_llm_provider
from autoscholar.rag import (
    FastEmbedReranker,
    FastEmbedSparseProvider,
    KnowledgeRepository,
    QdrantChunkIndex,
    RAGQueryService,
    RetrievalMode,
    RetrievedChunk,
)
from autoscholar.rag.worker import create_embedding_provider


@dataclass(frozen=True, slots=True)
class RAGBenchmarkItem:
    question: str
    relevant_document_ids: tuple[str, ...] = ()
    relevant_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RAGBenchmarkResult:
    mode: RetrievalMode
    count: int
    recall_at_k: dict[int, float]
    hit_rate_at_k: dict[int, float]
    mrr: float
    ndcg_at_k: dict[int, float]


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
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on benchmark line {line_number}") from exc
        question = str(payload.get("question") or "").strip()
        document_ids = _ids(payload, "relevant_document_ids", "relevant_document_id")
        chunk_ids = _ids(payload, "relevant_chunk_ids", "relevant_chunk_id")
        if not question or not (document_ids or chunk_ids):
            raise ValueError(
                f"Benchmark line {line_number} needs a question and relevant IDs"
            )
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
    normalized_ks = tuple(sorted(set(ks)))
    if not normalized_ks or normalized_ks[0] < 1:
        raise ValueError("Benchmark K values must be positive")
    max_k = normalized_ks[-1]
    recalls = {k: 0.0 for k in normalized_ks}
    hits = {k: 0.0 for k in normalized_ks}
    ndcgs = {k: 0.0 for k in normalized_ks}
    reciprocal_ranks = 0.0
    for item in items:
        retrieved = await retriever.retrieve(
            item.question,
            project_id=project_id,
            retrieval_mode=mode,
            top_k=max_k,
        )
        relevant_chunks = set(item.relevant_chunk_ids)
        relevant_documents = set(item.relevant_document_ids)
        ranked_ids = [
            chunk.id if relevant_chunks else chunk.document_id for chunk in retrieved
        ]
        relevant_ids = relevant_chunks or relevant_documents
        relevance = [identifier in relevant_ids for identifier in ranked_ids]
        first_rank = next(
            (rank for rank, is_relevant in enumerate(relevance, start=1) if is_relevant),
            None,
        )
        reciprocal_ranks += 1 / first_rank if first_rank is not None else 0.0
        for k in normalized_ks:
            prefix_ids = set(ranked_ids[:k])
            relevant_count = len(prefix_ids & relevant_ids)
            recalls[k] += relevant_count / len(relevant_ids)
            hits[k] += float(relevant_count > 0)
            gains = relevance[:k]
            dcg = sum(
                1 / math.log2(rank + 1)
                for rank, is_relevant in enumerate(gains, start=1)
                if is_relevant
            )
            ideal_count = min(len(relevant_ids), k)
            ideal_dcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
            ndcgs[k] += dcg / ideal_dcg if ideal_dcg else 0.0
    count = len(items)
    return RAGBenchmarkResult(
        mode=mode,
        count=count,
        recall_at_k={k: value / count for k, value in recalls.items()},
        hit_rate_at_k={k: value / count for k, value in hits.items()},
        mrr=reciprocal_ranks / count,
        ndcg_at_k={k: value / count for k, value in ndcgs.items()},
    )


def _ids(payload: dict[str, object], plural: str, singular: str) -> tuple[str, ...]:
    raw_plural = payload.get(plural)
    if isinstance(raw_plural, list):
        return tuple(dict.fromkeys(str(item) for item in raw_plural if str(item)))
    raw_singular = payload.get(singular)
    return (str(raw_singular),) if raw_singular else ()


async def _run_cli(args: argparse.Namespace, settings: Settings) -> None:
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
    args = _parser().parse_args()
    asyncio.run(_run_cli(args, get_settings()))


if __name__ == "__main__":
    main()
