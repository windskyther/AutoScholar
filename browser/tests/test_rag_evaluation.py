import json
from pathlib import Path

import pytest

from autoscholar.evaluation.rag import (
    RAGBenchmarkItem,
    evaluate_rag,
    load_rag_benchmark,
)
from autoscholar.rag.models import RetrievalMode, RetrievedChunk


class ScriptedRetriever:
    def __init__(self, results: dict[str, list[RetrievedChunk]]) -> None:
        self.results = results

    async def retrieve(
        self,
        question: str,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        retrieval_mode: RetrievalMode = "hybrid_rerank",
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        del project_id, document_ids, retrieval_mode
        return self.results[question][:top_k]


def chunk(chunk_id: str, document_id: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        id=chunk_id,
        project_id="project-1",
        document_id=document_id,
        title="Paper",
        page=1,
        section=None,
        content="Evidence",
        score=score,
        ordinal=0,
    )


async def test_rag_metrics_measure_chunk_and_document_relevance() -> None:
    retriever = ScriptedRetriever(
        {
            "chunk question": [
                chunk("wrong", "doc-x", 1.0),
                chunk("chunk-b", "doc-b", 0.9),
                chunk("chunk-a", "doc-a", 0.8),
            ],
            "document question": [chunk("other", "doc-c", 1.0)],
        }
    )
    items = [
        RAGBenchmarkItem(
            question="chunk question",
            relevant_chunk_ids=("chunk-a", "chunk-b"),
        ),
        RAGBenchmarkItem(
            question="document question",
            relevant_document_ids=("doc-c",),
        ),
    ]

    result = await evaluate_rag(
        retriever,
        project_id="project-1",
        items=items,
        mode="dense",
        ks=(1, 3),
    )

    assert result.count == 2
    assert result.recall_at_k[1] == 0.5
    assert result.recall_at_k[3] == 1.0
    assert result.hit_rate_at_k[1] == 0.5
    assert result.hit_rate_at_k[3] == 1.0
    assert result.mrr == pytest.approx(0.75)
    assert result.ndcg_at_k[3] == pytest.approx(0.8467, abs=0.001)


def test_benchmark_loader_accepts_singular_and_plural_ids(tmp_path: Path) -> None:
    dataset = tmp_path / "rag.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "question": "First",
                        "relevant_document_id": "document-1",
                    }
                ),
                json.dumps(
                    {
                        "question": "Second",
                        "relevant_chunk_ids": ["chunk-1", "chunk-2"],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    items = load_rag_benchmark(dataset)

    assert items[0].relevant_document_ids == ("document-1",)
    assert items[1].relevant_chunk_ids == ("chunk-1", "chunk-2")


def test_benchmark_loader_rejects_items_without_relevance_labels(tmp_path: Path) -> None:
    dataset = tmp_path / "invalid.jsonl"
    dataset.write_text('{"question":"Missing labels"}', encoding="utf-8")

    with pytest.raises(ValueError, match="relevant IDs"):
        load_rag_benchmark(dataset)
