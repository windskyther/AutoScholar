"""Matched descriptive contrasts; no unpaired averages or claims of significance."""

from collections.abc import Sequence
from pathlib import Path
from statistics import mean
from typing import Any

from autoscholar.evaluation.models import CaseResult
from autoscholar.evaluation.runner import _write_json

INTERVENTIONS = {
    "no_planner": "Validated static DAG replaces the root model Planner; child planning remains.",
    "no_reviewer": "Remove the model Reviewer only; deterministic review/Writer checks remain.",
    "no_memory": "Bypass actual public project-memory reads and learning; no invented experiences.",
    "no_replanning": "Stop before a replan; retain failed attempts and publish no report.",
    "no_reranker": "Skip lexical reranking of the same ordered Qdrant candidate pool.",
}


def comparison(results: Sequence[CaseResult], *, planned: int) -> dict[str, Any]:
    baseline = {
        (item.case_id, item.repeat, item.seed): item
        for item in results
        if item.adapter.variant == "baseline"
    }
    contrasts = []
    for variant in sorted({item.adapter.variant for item in results} - {"baseline"}):
        candidates = [item for item in results if item.adapter.variant == variant]
        matched = []
        comparable = []
        for item in candidates:
            reference = baseline.get((item.case_id, item.repeat, item.seed))
            if reference is None or (
                item.input_sha256 != reference.input_sha256
                or item.expected_sha256 != reference.expected_sha256
                or item.adapter.resources != reference.adapter.resources
                or item.adapter.category != reference.adapter.category
                or item.adapter.model != reference.adapter.model
                or item.adapter.name != reference.adapter.name
                or item.adapter.execution != reference.adapter.execution
            ):
                continue
            matched.append((reference, item))
            if reference.score is not None and item.score is not None:
                comparable.append((reference, item))
        metrics = {}
        for key in sorted(
            {
                name
                for left, right in comparable
                for case in (left, right)
                if case.score
                for name in case.score.metrics
            }
        ):
            differences: list[float] = []
            for left, right in comparable:
                assert left.score is not None and right.score is not None
                before, after = left.score.metrics.get(key), right.score.metrics.get(key)
                if before is not None and after is not None:
                    differences.append(after - before)
            metrics[key] = {
                "mean_delta": mean(differences) if differences else None,
                "paired": len(differences),
                "planned": planned,
            }
        usage = {}
        for key in ("model_calls", "budget_tokens", "external_api_calls", "total_tokens"):
            differences = []
            for left, right in matched:
                before, after = getattr(left.usage, key), getattr(right.usage, key)
                if before is not None and after is not None:
                    differences.append(after - before)
            usage[key] = {
                "mean_delta": mean(differences) if differences else None,
                "paired": len(differences),
                "planned": planned,
            }
        contrasts.append(
            {
                "variant": variant,
                "intervention": INTERVENTIONS[variant],
                "matched": len(matched),
                "scored_pairs": len(comparable),
                "planned": planned,
                "metrics": metrics,
                "usage": usage,
                "latency_ms": {
                    "mean_delta": mean(
                        right.duration_ms - left.duration_ms for left, right in matched
                    )
                    if matched
                    else None,
                    "paired": len(matched),
                    "planned": planned,
                },
            }
        )
    return {
        "schema_version": 1,
        "reference": "baseline",
        "delta_direction": "variant_minus_baseline",
        "interpretation": "Controlled engineering contrasts only; no significance or real "
        "LLM/neural-semantic claims. Reranker and workflow suites have separate denominators.",
        "contrasts": contrasts,
    }


def write_comparison(directory: Path, *, planned: int) -> None:
    results = [
        CaseResult.model_validate_json(line)
        for line in (directory / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    payload = comparison(results, planned=planned)
    _write_json(directory / "ablation_comparison.json", payload)
    with (directory / "evaluation_report.md").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Controlled ablations\n\n")
        stream.write(
            "Paired deltas (variant minus baseline) and scored/planned coverage are in "
            "`ablation_comparison.json`. Inputs, expected labels and resources must match. "
            "Missing measurements remain unknown. These are engineering contrasts, not "
            "model ability or statistically significant findings.\n\n"
        )
        stream.write(
            "| Variant | Scored pairs / planned | Task-completion delta | "
            "Mean callback delta |\n|---|---:|---:|---:|\n"
        )
        for contrast in payload["contrasts"]:
            task = contrast["metrics"].get("task_completion", {}).get("mean_delta")
            callbacks = contrast["usage"]["model_calls"]["mean_delta"]
            task_display = "unknown" if task is None else f"{task:.6g}"
            call_display = "unknown" if callbacks is None else f"{callbacks:.6g}"
            stream.write(
                f"| {contrast['variant']} | {contrast['scored_pairs']} / "
                f"{planned} | {task_display} | {call_display} |\n"
            )
        stream.write("\n")
        for variant in sorted({item.adapter.variant for item in results} - {"baseline"}):
            stream.write(f"- `{variant}`: {INTERVENTIONS[variant]}\n")
