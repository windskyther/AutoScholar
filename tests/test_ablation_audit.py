"""Statistical/evidence contracts only; synthetic scores are not model measurements."""

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.core.config import Settings
from autoscholar.evaluation import ablation_audit as audit
from autoscholar.evaluation.datasets import payload_digest
from tests.test_evaluation_neural import REVISION, synthetic_results


@pytest.mark.parametrize(
    "groups, expected",
    [([[0.0], [0.0]], 1.0), ([[1.0], [1.0]], 0.5), ([[1.0], [-1.0]], 1.0), ([[1.0]], 1.0)],
)
def test_exact_sign_flip(groups: list[list[float]], expected: float) -> None:
    assert audit.exact_sign_flip(groups) == expected
    assert audit.exact_sign_flip([[-x for x in group] for group in groups]) == expected


@pytest.mark.parametrize("groups", [[], [[]], [[float("nan")]], [[float("inf")]], [[1.0]] * 21])
def test_invalid_flip_input(groups: list[list[float]]) -> None:
    with pytest.raises(ValueError):
        audit.exact_sign_flip(groups)


def test_holm_and_bootstrap() -> None:
    assert audit.holm_adjust([0.02, 0.01, 0.5]) == [0.04, 0.03, 0.5]
    assert audit.holm_adjust([0.1, 0.1, 0.1]) == pytest.approx([0.3, 0.3, 0.3])
    assert audit.clustered_interval([[0.25], [0.25, 0.25]]) == [0.25, 0.25]
    groups = [[0.0], [0.5, 0.5]]
    assert audit.clustered_interval(groups) == audit.clustered_interval(groups)
    with pytest.raises(ValueError):
        audit.clustered_interval([[]])


@pytest.mark.parametrize("effect", [0.0, 0.25, -0.25])
def test_repeat_collapse(effect: float) -> None:
    suite, manifest, base = synthetic_results()
    manifest.configuration.repeats = 2
    results = []
    for repeat in (1, 2):
        for original in base:
            row = original.model_copy(deep=True, update={"repeat": repeat, "seed": 41 + repeat})
            assert row.score is not None
            row.score.checks["paired_hybrid_pool"] = True
            if row.adapter.variant in ("hybrid", "hybrid_rerank"):
                for metric in audit.METRICS:
                    row.score.metrics[metric] = 0.5 + (
                        effect if row.adapter.variant == "hybrid_rerank" else 0
                    )
            results.append(row)
    report = audit.paired_statistics(suite, manifest, results)
    assert report["complete"] and report["matched_repeat_pairs"] == 40
    assert report["unique_queries"] == 20 and report["gold_document_blocks"] == 16
    for item in report["metrics"].values():
        assert item["mean_delta"] == effect and item["query_units_scored"] == 20
        assert item["significant_positive_effect"] is (effect > 0)


@pytest.mark.parametrize(
    "fault", ["duplicate", "missing", "gold", "resources", "model", "pool", "metric"]
)
def test_bad_pair(fault: str) -> None:
    suite, manifest, results = synthetic_results()
    for row in results:
        assert row.score is not None
        row.score.checks["paired_hybrid_pool"] = True
    selected = next(row for row in results if row.adapter.variant == "hybrid_rerank")
    assert selected.score is not None
    if fault == "duplicate":
        results.append(selected.model_copy(deep=True))
    elif fault == "missing":
        results.remove(selected)
    elif fault == "gold":
        selected.expected_sha256 = "b" * 64
    elif fault == "resources":
        selected.adapter = selected.adapter.model_copy(
            deep=True, update={"resources": {"different": "b" * 64}}
        )
    elif fault == "model":
        selected.adapter = selected.adapter.model_copy(deep=True, update={"model": "different"})
    elif fault == "pool":
        selected.score.checks["paired_hybrid_pool"] = False
    else:
        selected.score.metrics["chunk_mrr"] = None
    if fault in ("missing", "metric"):
        report = audit.paired_statistics(suite, manifest, results)
        assert not report["complete"]
        assert all(item["holm_adjusted_p"] is None for item in report["metrics"].values())
    else:
        with pytest.raises(ValueError):
            audit.paired_statistics(suite, manifest, results)


def test_overlapping_gold_blocks() -> None:
    suite, _, _ = synthetic_results()
    suite.cases[0].expected["relevant_document_ids"] = ["shared", "one"]
    suite.cases[1].expected["relevant_document_ids"] = ["shared", "two"]
    suite.cases[2].expected["relevant_document_ids"] = ["two", "three"]
    blocks = audit.document_blocks(suite)
    assert blocks[suite.cases[0].id] == blocks[suite.cases[1].id] == blocks[suite.cases[2].id]


def test_offline_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, manifest, results = synthetic_results()
    for row in results:
        assert row.score is not None
        row.score.checks["paired_hybrid_pool"] = True
    root = tmp_path / "data/evaluation"
    semantic = root / "semantic"
    semantic.mkdir(parents=True)
    (semantic / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (semantic / "cases.jsonl").write_text(
        "\n".join(row.model_dump_json() for row in results), encoding="utf-8"
    )
    monkeypatch.setattr(audit, "_git_state", lambda *args: (REVISION, False))
    monkeypatch.setattr(
        audit,
        "inspect_engineering",
        lambda *args, **kwargs: ({"engineering_acceptance": True, "slots": []}, {}),
    )
    monkeypatch.setattr(
        audit,
        "inspect_semantic",
        lambda *args, **kwargs: (
            {"engineering_acceptance": True, "semantic_quality_acceptance": True},
            {},
        ),
    )
    monkeypatch.setattr(
        audit, "load_suite", lambda *args: (deepcopy(suite), payload_digest(suite.model_dump()))
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Audit must not create clients/load settings")

    monkeypatch.setattr(Settings, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    directory, accepted = audit.run_ablation_audit(
        argparse.Namespace(engineering_run=root, semantic_run=semantic), repo_root=tmp_path
    )
    assert accepted
    summary = json.loads((directory / "ablation_acceptance.json").read_text(encoding="utf-8"))
    assert (
        summary["actual_vendor_requests_this_audit"]
        == summary["actual_inference_runs_this_audit"]
        == 0
    )
    assert (
        summary["phase10_overall_acceptance"] is False and summary["main_merge_authorized"] is False
    )
