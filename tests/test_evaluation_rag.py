import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, ValidationError

from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    BenchmarkSuite,
    MeasuredUsage,
    Observation,
    RunConfiguration,
)
from autoscholar.evaluation.rag import RAGBenchmarkItem, evaluate_rag, load_rag_benchmark
from autoscholar.evaluation.rag_adapter import (
    MODES,
    InjectedRAGAdapter,
    RankingFixture,
    ReplayRAGAdapter,
    load_ranking_fixture,
    score_observation,
)
from autoscholar.evaluation.ranking import aggregate_rankings, normalize_ks, score_ranking
from autoscholar.evaluation.runner import run_evaluation
from autoscholar.rag.models import RetrievalMode, RetrievedChunk

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks/rag/public_v1.json"
FIXTURE = ROOT / "benchmarks/rag/ranking_fixture_v1.json"


class Retriever:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, str, RetrievalMode, int | None]] = []

    async def retrieve(
        self,
        question: str,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        retrieval_mode: RetrievalMode = "hybrid_rerank",
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        self.calls.append((question, project_id, retrieval_mode, top_k))
        return self.chunks  # Deliberately exercise providers that ignore their limit.


def chunk(identifier: str, document: str = "doc-a") -> RetrievedChunk:
    return RetrievedChunk(
        id=identifier,
        document_id=document,
        project_id="public-project",
        title="Self-authored",
        page=1,
        section=None,
        content="Public text",
        score=1.0,
        ordinal=0,
    )


def test_duplicate_gains_do_not_inflate_ndcg_and_do_not_compress_positions() -> None:
    duplicate = score_ranking(["a", "a", "a"], ["a"], ks=[1, 3])
    assert duplicate.ndcg_at_k == {1: 1.0, 3: 1.0}
    result = score_ranking(["wrong", "a", "a", "b"], ["a", "b"], ks=[3, 4])
    assert result.recall_at_k == {3: 0.5, 4: 1.0}
    assert result.mrr == 0.5
    assert result.ndcg_at_k[3] == pytest.approx((1 / math.log2(3)) / (1 + 1 / math.log2(3)))
    assert result.ndcg_at_k[4] == pytest.approx(
        (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3))
    )


def test_empty_ranking_and_truncated_mrr_have_real_zero_scores() -> None:
    empty = score_ranking([], ["a"], ks=[1, 2])
    assert empty.mrr == 0.0 and empty.ndcg_at_k[2] == 0.0
    ignored = score_ranking(["wrong", "a"], ["a"], ks=[1])
    assert ignored.mrr == 0.0
    assert ignored.recall_at_k[1] == 0.0


@pytest.mark.parametrize("ks", [[], [0], [-1], [True], [1.5], [51], list(range(1, 12))])
def test_k_validation_rejects_invalid_and_unbounded_cutoffs(ks: list[int]) -> None:
    with pytest.raises(ValueError):
        normalize_ks(ks)


def test_aggregation_keeps_label_count_and_requires_same_cutoffs() -> None:
    first = score_ranking(["a"], ["a"], ks=[1])
    other = score_ranking([], ["a"], ks=[1])
    result = aggregate_rankings([first, other])
    assert result is not None and result.count == 2 and result.mrr == 0.5
    assert aggregate_rankings([]) is None
    with pytest.raises(ValueError):
        aggregate_rankings([first, score_ranking([], ["a"], ks=[2])])


async def test_legacy_evaluator_scores_both_levels_and_keeps_compatibility_fields() -> None:
    result = await evaluate_rag(
        Retriever([chunk("a1"), chunk("a2"), chunk("b1", "doc-b")]),
        project_id="public-project",
        items=[
            RAGBenchmarkItem(
                "public", relevant_document_ids=("doc-a", "doc-b"), relevant_chunk_ids=("b1",)
            )
        ],
        mode="dense",
        ks=[1, 3],
    )
    assert result.mrr == pytest.approx(1 / 3)
    assert result.document_metrics is not None and result.chunk_metrics is not None
    assert result.document_metrics.mrr == 1.0
    assert result.document_metrics.recall_at_k[1] == 0.5
    assert result.document_metrics.count == result.chunk_metrics.count == 1
    assert result.document_metrics.ndcg_at_k[3] <= 1.0


async def test_unlabeled_level_is_unknown_and_foreign_project_is_rejected() -> None:
    item = RAGBenchmarkItem("public", relevant_chunk_ids=("a1",))
    result = await evaluate_rag(
        Retriever([chunk("a1")]), project_id="public-project", items=[item], mode="dense", ks=[1]
    )
    assert result.document_metrics is None and result.chunk_metrics is not None
    with pytest.raises(ValueError, match="another project"):
        await evaluate_rag(
            Retriever([replace(chunk("a1"), project_id="foreign")]),
            project_id="public-project",
            items=[item],
            mode="dense",
        )


@pytest.mark.parametrize(
    "line",
    [
        "[]",
        '{"question":42,"relevant_chunk_id":"a"}',
        '{"question":"q","relevant_chunk_ids":[null]}',
        '{"question":"q","relevant_chunk_id":true}',
        '{"question":"q","relevant_chunk_ids":[""]}',
        '{"question":"q","relevant_chunk_ids":["a"],"relevant_chunk_id":"b"}',
        '{"question":"q","question":"changed","relevant_chunk_id":"a"}',
    ],
)
def test_legacy_jsonl_rejects_ambiguous_and_coerced_gold_labels(tmp_path: Path, line: str) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ValueError):
        load_rag_benchmark(path)


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_schema_version_is_not_a_coerced_literal(version: object) -> None:
    suite, _ = load_suite(DATASET)
    payload = suite.model_dump()
    payload["schema_version"] = version
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(payload)


def test_public_data_has_twenty_unique_bound_queries_and_known_labels() -> None:
    suite, _ = load_suite(DATASET)
    fixture, digest = load_ranking_fixture(FIXTURE)
    assert len(suite.cases) == len(fixture.queries) == 20
    assert fixture.suite_id == suite.id
    for mode in MODES:
        adapter = ReplayRAGAdapter(fixture, fixture_sha256=digest, mode=mode)
        for case in suite.cases:
            adapter.validate_case(case)


def test_fixture_rejects_unknown_candidates_and_query_bindings() -> None:
    fixture, _ = load_ranking_fixture(FIXTURE)
    payload = fixture.model_dump()
    first = next(iter(payload["rankings"]))
    payload["rankings"][first]["dense"] = ["unknown"]
    with pytest.raises(ValueError):
        RankingFixture.model_validate(payload)
    payload = fixture.model_dump()
    payload["queries"].pop(first)
    with pytest.raises(ValueError):
        RankingFixture.model_validate(payload)


async def test_all_replay_modes_produce_records_with_real_zero_external_usage(
    tmp_path: Path,
) -> None:
    suite, digest = load_suite(DATASET)
    fixture, fixture_digest = load_ranking_fixture(FIXTURE)
    destination = await run_evaluation(
        suite,
        dataset_sha256=digest,
        adapters=[
            ReplayRAGAdapter(fixture, fixture_sha256=fixture_digest, mode=mode) for mode in MODES
        ],
        configuration=RunConfiguration(),
        output_root=tmp_path,
        repo_root=ROOT,
    )
    records = [
        json.loads(line)
        for line in (destination / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 80
    assert all(
        result["status"] == "passed" and result["usage"]["external_api_calls"] == 0
        for result in records
    )
    assert all(result["usage"]["monetary_cost"] is None for result in records)
    assert all(
        0 <= value <= 1 for result in records for value in result["score"]["metrics"].values()
    )
    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    assert all(group["task_success_rate"] is None for group in summary["groups"])
    report = (destination / "evaluation_report.md").read_text(encoding="utf-8")
    assert "## Aggregated metrics" in report and "chunk_mrr" in report
    assert "## Latency" in report and "20 / 20" in report


def test_replay_preflight_rejects_changed_prompts_and_wrong_level_labels() -> None:
    suite, _ = load_suite(DATASET)
    fixture, digest = load_ranking_fixture(FIXTURE)
    adapter = ReplayRAGAdapter(fixture, fixture_sha256=digest, mode="dense")
    case = suite.cases[0].model_copy(deep=True)
    case.prompt = "different public question"
    with pytest.raises(ValueError, match="query"):
        adapter.validate_case(case)
    case = suite.cases[0].model_copy(deep=True)
    case.expected["relevant_document_ids"] = ["public-workflow"]
    with pytest.raises(ValueError, match="disagree"):
        adapter.validate_case(case)


def test_empty_results_fail_checks_instead_of_being_treated_as_success() -> None:
    expected: dict[str, JsonValue] = {"relevant_chunk_ids": ["a"]}
    score = score_observation(Observation(payload={"ranking": []}), expected, [5, 10])
    assert score.checks == {"chunk_hit": False}
    assert score.metrics["chunk_mrr"] == 0.0
    assert "document_mrr" not in score.metrics


async def test_injected_retriever_gets_only_question_and_scope_and_usage_stays_unknown() -> None:
    retriever = Retriever([chunk("a")])
    adapter = InjectedRAGAdapter(
        retriever,
        project_id="public-project",
        mode="hybrid",
        identity=AdapterIdentity(
            name="explicit-components", category="rag", variant="hybrid", execution="injected"
        ),
    )
    case = BenchmarkCase(
        id="public-case",
        prompt="public question",
        inputs={"query_id": "q"},
        expected={"relevant_chunk_ids": ["a"]},
    )
    adapter.validate_case(case)
    result = await adapter.execute(case.prompt, case.inputs, seed=42)
    assert retriever.calls == [("public question", "public-project", "hybrid", 10)]
    assert result.usage == MeasuredUsage()
    assert adapter.score(result, case.expected).checks == {"chunk_hit": True}


def arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluation", "run", "--dataset", str(DATASET), "--fixture", str(FIXTURE), *extra],
    )


def test_cli_does_not_construct_http_clients_or_load_application_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autoscholar.core.config import Settings

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Provider/Settings must not be instantiated by fixture replay")

    monkeypatch.setattr(Settings, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setenv("LLM_API_KEY", "private-do-not-use")
    arguments(tmp_path, monkeypatch, "--modes", "dense")
    assert cli.main() == 0
    assert "NOT real retrieval" in capsys.readouterr().out
    assert len(list((tmp_path / "data/evaluation").iterdir())) == 1


@pytest.mark.parametrize(
    "extra",
    [
        ("--ks", "0"),
        ("--modes", "dense", "dense"),
        ("--repeats", "0"),
        ("--output-root", "../outside"),
    ],
)
def test_cli_invalid_configuration_never_creates_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
) -> None:
    arguments(tmp_path, monkeypatch, *extra)
    assert cli.main() == 2
    assert not (tmp_path / "data/evaluation").exists()


def test_cli_failed_retrieval_check_returns_nonzero_and_retains_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, _ = load_ranking_fixture(FIXTURE)
    payload = fixture.model_dump()
    payload["rankings"]["mnist-subset"]["dense"] = []
    custom = tmp_path / "fixture.json"
    custom.write_text(json.dumps(payload), encoding="utf-8")
    arguments(tmp_path, monkeypatch, "--fixture", str(custom), "--modes", "dense")
    assert cli.main() == 1
    directory = next((tmp_path / "data/evaluation").iterdir())
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["groups"][0]["status_counts"] == {"failed": 1, "passed": 19}
    assert summary["groups"][0]["check_pass_rate"] == 0.95


async def test_repeat_seed_wrap_is_recorded_within_supported_range(tmp_path: Path) -> None:
    suite, digest = load_suite(DATASET)
    fixture, fixture_digest = load_ranking_fixture(FIXTURE)
    suite.cases = suite.cases[:1]
    directory = await run_evaluation(
        suite,
        dataset_sha256=digest,
        adapters=[ReplayRAGAdapter(fixture, fixture_sha256=fixture_digest, mode="dense")],
        configuration=RunConfiguration(seed=2**32 - 1, repeats=2),
        output_root=tmp_path,
        repo_root=ROOT,
    )
    records = [
        json.loads(line)
        for line in (directory / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["seed"] for record in records] == [2**32 - 1, 0]


async def test_offline_runner_rejects_injected_components_before_any_execution(
    tmp_path: Path,
) -> None:
    suite, digest = load_suite(DATASET)
    retriever = Retriever([chunk("a")])
    adapter = InjectedRAGAdapter(
        retriever,
        project_id="public-project",
        mode="dense",
        identity=AdapterIdentity(
            name="explicit-components", category="rag", variant="dense", execution="injected"
        ),
    )
    with pytest.raises(ValueError, match="Offline"):
        await run_evaluation(
            suite,
            dataset_sha256=digest,
            adapters=[adapter],
            configuration=RunConfiguration(),
            output_root=tmp_path / "runs",
            repo_root=ROOT,
        )
    assert not retriever.calls and not (tmp_path / "runs").exists()
