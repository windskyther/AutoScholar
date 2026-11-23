"""Offline, post-hoc paired ablation review; no .env, inference or vendor requests."""

import argparse
import hashlib
import math
from itertools import product
from pathlib import Path
from random import Random
from statistics import mean
from typing import Any
from uuid import uuid4

from autoscholar.evaluation.datasets import decode_json, load_suite, payload_digest, read_bounded
from autoscholar.evaluation.models import BenchmarkSuite, CaseResult, RunManifest
from autoscholar.evaluation.phase10_closure import inspect_engineering, inspect_semantic
from autoscholar.evaluation.rag_adapter import RAGLabels
from autoscholar.evaluation.runner import _git_state, _write_json

METRICS = ("chunk_mrr", "chunk_recall_at_5", "chunk_ndcg_at_5")
ALPHA = 0.05
BOOTSTRAP_REPLICATES = 4096
STATISTICS_SEED = 20261013


def clustered_interval(groups: list[list[float]]) -> list[float]:
    if not groups or any(not group or not all(math.isfinite(x) for x in group) for group in groups):
        raise ValueError("Expected nonempty finite query blocks")
    rng = Random(STATISTICS_SEED)
    sampled = []
    for _ in range(BOOTSTRAP_REPLICATES):
        draw = [groups[rng.randrange(len(groups))] for _ in groups]
        sampled.append(sum(sum(group) for group in draw) / sum(len(group) for group in draw))
    sampled.sort()

    # Linear percentile interpolation, fixed before this audit implementation is run.
    def percentile(probability: float) -> float:
        index = (len(sampled) - 1) * probability
        lower = math.floor(index)
        return sampled[lower] + (sampled[math.ceil(index)] - sampled[lower]) * (index - lower)

    return [percentile(0.025), percentile(0.975)]


def exact_sign_flip(groups: list[list[float]]) -> float:
    if (
        not groups
        or len(groups) > 20
        or any(not group or not all(math.isfinite(x) for x in group) for group in groups)
    ):
        raise ValueError("Exact sign-flip protocol allows 1..20 finite nonempty blocks")
    sums = [sum(group) for group in groups]
    observed = abs(sum(sums))
    nonzero = [value for value in sums if value != 0]
    if not nonzero:
        return 1.0
    extreme = sum(
        abs(sum(sign * value for sign, value in zip(signs, nonzero, strict=True)))
        >= observed - 1e-12
        for signs in product((-1, 1), repeat=len(nonzero))
    )
    return extreme / (1 << len(nonzero))


def holm_adjust(values: list[float]) -> list[float]:
    if not values or any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        raise ValueError("Expected finite p-values")
    adjusted = [0.0] * len(values)
    previous = 0.0
    for rank, index in enumerate(sorted(range(len(values)), key=values.__getitem__)):
        previous = max(previous, min(1.0, (len(values) - rank) * values[index]))
        adjusted[index] = previous
    return adjusted


def document_blocks(suite: BenchmarkSuite) -> dict[str, str]:
    # Connected components, not tuple keys: overlapping multi-document labels share a block.
    labels = {
        case.id: set(RAGLabels.model_validate(case.expected).relevant_document_ids)
        for case in suite.cases
    }
    if any(not docs for docs in labels.values()):
        raise ValueError("Document-block statistics need independent public document labels")
    remaining = set(labels)
    blocks = {}
    while remaining:
        members = {min(remaining)}
        docs = set(labels[next(iter(members))])
        while True:
            added = {qid for qid in remaining - members if docs & labels[qid]}
            if not added:
                break
            members |= added
            docs |= set.union(*(labels[qid] for qid in added))
        identifier = payload_digest(sorted(docs))
        blocks.update({qid: identifier for qid in members})
        remaining -= members
    return blocks


def paired_statistics(
    suite: BenchmarkSuite, manifest: RunManifest, results: list[CaseResult]
) -> dict[str, Any]:
    scheduled = {case.id for case in suite.cases}
    by_id = {case.id: case for case in suite.cases}
    expected = {
        (qid, repeat)
        for qid in scheduled
        for repeat in range(1, manifest.configuration.repeats + 1)
    }
    selected: dict[str, dict[tuple[str, int], CaseResult]] = {"hybrid": {}, "hybrid_rerank": {}}
    for row in results:
        if row.adapter.variant not in selected:
            continue
        key = row.case_id, row.repeat
        if key not in expected or key in selected[row.adapter.variant]:
            raise ValueError("Unexpected or duplicate neural pair")
        case = by_id[row.case_id]
        if (
            row.input_sha256 != payload_digest({"prompt": case.prompt, "inputs": case.inputs})
            or row.expected_sha256 != payload_digest(case.expected)
            or row.seed != (manifest.configuration.seed + row.repeat - 1) % (2**32)
        ):
            raise ValueError("Neural row differs from its scheduled public case")
        selected[row.adapter.variant][key] = row
    blocks = document_blocks(suite)
    queries: dict[str, dict[str, list[float]]] = {metric: {} for metric in METRICS}
    matched = 0
    for key in sorted(expected):
        before, after = selected["hybrid"].get(key), selected["hybrid_rerank"].get(key)
        if before is None or after is None:
            continue
        if (
            before.seed != after.seed
            or before.input_sha256 != after.input_sha256
            or before.expected_sha256 != after.expected_sha256
            or before.adapter.resources != after.adapter.resources
            or before.adapter.model != after.adapter.model
            or before.adapter.name != after.adapter.name
            or before.adapter.execution != after.adapter.execution
            or before.category != after.category
        ):
            raise ValueError("Neural contrast input/gold/seed/resource identity differs")
        if (
            before.status != "passed"
            or after.status != "passed"
            or before.score is None
            or after.score is None
        ):
            continue
        if (
            before.score.checks.get("paired_hybrid_pool") is not True
            or after.score.checks.get("paired_hybrid_pool") is not True
        ):
            raise ValueError("Same candidate-pool verification is missing")
        matched += 1
        for metric in METRICS:
            left, right = before.score.metrics.get(metric), after.score.metrics.get(metric)
            if left is not None and right is not None:
                if not 0 <= left <= 1 or not 0 <= right <= 1:
                    raise ValueError("Bounded ranking metric is invalid")
                queries[metric].setdefault(key[0], []).append(right - left)
    complete = matched == len(expected)
    summaries: dict[str, Any] = {}
    pvalues = []
    for metric in METRICS:
        values = queries[metric]
        scored = {
            qid: mean(rows)
            for qid, rows in values.items()
            if len(rows) == manifest.configuration.repeats
        }
        complete_metric = len(scored) == len(scheduled)
        complete = complete and complete_metric
        grouped: dict[str, list[float]] = {}
        for qid, delta in sorted(scored.items()):
            grouped.setdefault(blocks[qid], []).append(delta)
        groups = list(grouped.values())
        pvalue = exact_sign_flip(groups) if complete_metric else None
        if pvalue is not None:
            pvalues.append(pvalue)
        summaries[metric] = {
            "mean_delta": mean(scored.values()) if scored else None,
            "query_units_scored": len(scored),
            "query_units_planned": len(scheduled),
            "gold_document_blocks": len(groups),
            "cluster_bootstrap_95_interval": clustered_interval(groups)
            if complete_metric
            else None,
            "two_sided_exact_cluster_p": pvalue,
            "holm_adjusted_p": None,
            "significant_positive_effect": None,
        }
    if complete:
        for metric, adjusted in zip(METRICS, holm_adjust(pvalues), strict=True):
            item = summaries[metric]
            item["holm_adjusted_p"] = adjusted
            item["significant_positive_effect"] = (
                adjusted < ALPHA and item["cluster_bootstrap_95_interval"][0] > 0
            )
    return {
        "complete": complete,
        "delta_direction": "hybrid_rerank_minus_hybrid",
        "matched_repeat_pairs": matched,
        "planned_repeat_pairs": len(expected),
        "unique_queries": len(scheduled),
        "gold_document_blocks": len(set(blocks.values())),
        "repeats_are_not_independent_samples": True,
        "metrics": summaries,
        "protocol": {
            "alpha": ALPHA,
            "family_metrics": METRICS,
            "correction": "Holm",
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "bootstrap_seed": STATISTICS_SEED,
            "unit": "query_mean_across_repeats",
            "block": "connected_components_of_shared_gold_documents",
        },
        "interpretation": "Post-hoc conditional analysis of a fixed self-authored public corpus. "
        "Gold-document blocks reduce shared-source dependence, not prove independence. "
        "Intervals assume exchangeable document blocks; sign flips assume symmetric null effects. "
        "No generalization, causal superiority or independent replication is claimed.",
    }


def run_ablation_audit(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    revision, dirty = _git_state(repo_root)
    if not revision or dirty is not False:
        raise ValueError("Final ablation audit requires clean committed source")
    root = (repo_root / "data/evaluation").resolve()
    if not root.is_relative_to(repo_root.resolve()):
        raise ValueError("Audit output escaped evaluation root")
    engineering, engineering_hashes = inspect_engineering(args.engineering_run, repo_root=repo_root)
    semantic, semantic_hashes = inspect_semantic(args.semantic_run, repo_root=repo_root)
    directory = args.semantic_run.resolve()
    manifest = RunManifest.model_validate(decode_json(read_bounded(directory / "manifest.json")))
    results = [
        CaseResult.model_validate(decode_json(line))
        for line in read_bounded(directory / "cases.jsonl").splitlines()
    ]
    suite, _ = load_suite(repo_root / "benchmarks/semantic/public_v1.json")
    neural = paired_statistics(suite, manifest, results)
    slots = [
        slot
        for slot in engineering["slots"]
        if slot["id"] in ("workflow_ablations", "retrieval_ablations")
    ]
    hashes = {}
    for folder, bindings in (
        (args.engineering_run.resolve(), engineering_hashes),
        (directory, semantic_hashes),
    ):
        for name, sha in bindings.items():
            path = folder / name
            if hashlib.sha256(read_bounded(path)).hexdigest() != sha:
                raise ValueError("Primary ablation evidence changed during audit")
            hashes[path.relative_to(root).as_posix()] = sha
    unchanged = _git_state(repo_root) == (revision, False)
    accepted = bool(
        engineering["engineering_acceptance"]
        and semantic["engineering_acceptance"]
        and neural["complete"]
        and unchanged
    )
    summary = {
        "schema_version": 1,
        "git_revision": revision,
        "source_unchanged": unchanged,
        "final_free_ablation_review_acceptance": accepted,
        "phase10_overall_acceptance": False,
        "main_merge_authorized": False,
        "actual_vendor_requests_this_audit": 0,
        "actual_inference_runs_this_audit": 0,
        "new_independent_samples": 0,
        "statistical_protocol_preregistered": False,
        "historical_engineering": slots,
        "historical_neural_statistics": neural,
        "semantic_quality_gate": semantic["semantic_quality_acceptance"],
        "primary_sha256": hashes,
        "unverified": ["real_llm_workflow_ablations", "autonomous_real_llm_end_to_end"],
        "scope": "Offline primary-evidence audit + post-hoc cached-model reranker statistics. "
        "Scripted workflow contrasts are not LLM intelligence experiments. No API or model replay.",
    }
    output = root / ("ablation-audit-" + uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "ablation_acceptance.json", summary)
    lines = [
        "# Final free ablation review",
        "",
        f"Evidence/statistics checks accepted: {accepted}.",
        "",
        summary["scope"],
        "",
        "No new model/API requests. Original primary files remain unchanged.",
        "",
        "## Actual cached reranker (post-hoc)",
        "",
        neural["interpretation"],
        "",
        f"Matched repeats {neural['matched_repeat_pairs']}/{neural['planned_repeat_pairs']}; "
        f"queries {neural['unique_queries']}; document blocks {neural['gold_document_blocks']}.",
        "",
        "| Metric | Mean delta | 95% block-bootstrap interval | Exact p | Holm p |",
        "|---|---:|---|---:|---:|",
    ]
    for metric, item in neural["metrics"].items():
        lines.append(
            f"| {metric} | {item['mean_delta']} | {item['cluster_bootstrap_95_interval']} | "
            f"{item['two_sided_exact_cluster_p']} | {item['holm_adjusted_p']} |"
        )
    lines += [
        "",
        "## Controlled workflow contrasts",
        "",
        "Scripted decisions, not statistical LLM effects.",
        "",
    ]
    for slot in slots:
        for contrast in slot["ablations"]["contrasts"]:
            lines.append(
                f"- {contrast['variant']}: {contrast['scored_pairs']}/{contrast['planned']} pairs; "
                f"intervention: {contrast['intervention']}"
            )
    lines += [
        "",
        "No gain or a nonsignificant result is not an infrastructure failure. "
        "Do not promote this to full Phase 10 or approve main. Real LLM workflow ablations "
        "require a separately bounded implementation and new explicit paid approval.",
        "",
    ]
    (output / "evaluation_report.md").write_text("\n".join(lines), encoding="utf-8")
    return output, accepted
