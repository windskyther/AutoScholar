"""Aggregate all outcomes, preserving coverage and unknown measured values."""

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from statistics import mean
from typing import TypedDict

from autoscholar.evaluation.models import CaseResult, MeasuredUsage, RunManifest


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


class MetricStatistics(TypedDict):
    mean: float | None
    scored: int
    planned: int


def _metrics(cases: Sequence[CaseResult], planned: int) -> dict[str, MetricStatistics]:
    names = sorted({key for result in cases if result.score for key in result.score.metrics})
    result: dict[str, MetricStatistics] = {}
    for name in names:
        numbers = [
            score.metrics[name]
            for case in cases
            if (score := case.score) and score.metrics.get(name) is not None
        ]
        known = [number for number in numbers if number is not None]
        result[name] = {
            "mean": mean(known) if known else None,
            "scored": len(known),
            "planned": planned,
        }
    return result


def _display(value: int | float | None) -> str:
    return "unknown" if value is None else f"{value:.6g}"


def summarize(
    manifest: RunManifest, results: Sequence[CaseResult], planned_per_variant: int
) -> dict[str, object]:
    groups: list[dict[str, object]] = []
    for adapter in manifest.adapters:
        cases = [result for result in results if result.adapter.variant == adapter.variant]
        metric_summary = _metrics(cases, planned_per_variant)
        usage: dict[str, int | float | None] = {}
        for key in MeasuredUsage.model_fields:
            usage_values = [getattr(result.usage, key) for result in cases]
            known = [value for value in usage_values if value is not None]
            # A total is unknown if any scheduled case has missing usage or did not run.
            usage[key] = (
                sum(known)
                if len(cases) == planned_per_variant
                and usage_values
                and len(known) == len(usage_values)
                else None
            )
        task_success = None
        if adapter.category == "end_to_end":
            task_success = (
                sum(bool(result.score and result.score.task_success is True) for result in cases)
                / planned_per_variant
            )
        groups.append(
            {
                "variant": adapter.variant,
                "execution": adapter.execution,
                "planned": planned_per_variant,
                "recorded": len(cases),
                "status_counts": dict(Counter(result.status for result in cases)),
                "check_pass_rate": sum(result.status == "passed" for result in cases)
                / planned_per_variant,
                "task_success_rate": task_success,
                "metrics": metric_summary,
                "usage": usage,
                "mean_duration_ms": mean(result.duration_ms for result in cases) if cases else None,
            }
        )
    return {
        "schema_version": 1,
        "run_id": manifest.run_id,
        "state": manifest.state,
        "suite_id": manifest.suite_id,
        "groups": groups,
    }


def write_report(
    destination: Path, manifest: RunManifest, results: Sequence[CaseResult], planned: int
) -> None:
    from autoscholar.evaluation.runner import _write_json

    summary = summarize(manifest, results, planned)
    _write_json(destination / "summary.json", summary)
    lines = [
        "# AutoScholar-Eval",
        "",
        f"Run: `{manifest.run_id}`",
        f"Suite: `{manifest.suite_id}`; state: `{manifest.state}`",
        f"Profile: `{manifest.configuration.profile}`; "
        f"seed: `{manifest.configuration.seed}`; repeats: `{manifest.configuration.repeats}`",
        f"Code: `{manifest.git_revision or 'unknown'}`; dirty: `{manifest.git_dirty}`",
        f"Dataset SHA-256: `{manifest.dataset_sha256}`",
        "",
        "## Interpretation",
        "",
        "Fixture results measure engineering/scorer behavior, NOT real model, retrieval, or "
        "provider ability. Injected-component results must identify their actual components.",
        "Checks passing is not task success. Only E2E adapters score task completion.",
        "All scheduled cases remain in the check-pass denominator, including errors/timeouts. "
        "Metric means show scored/planned coverage; unavailable values are unknown, not zero.",
        "Durations include execution and scoring, exclude environment setup and report writing. "
        "Small-sample differences are descriptive, not statistically significant findings.",
        "Measured usage is separate from budget counters. No pricing assumptions are made.",
        "A dirty working tree cannot be reproduced from the Git revision alone. "
        "Use a clean committed source tree for comparable benchmark results.",
        "",
        "## Coverage",
        "",
        "| Variant | Execution | Recorded / planned | Passed | Failed | "
        "Timeout | Error | Cancelled |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for adapter in manifest.adapters:
        cases = [result for result in results if result.adapter.variant == adapter.variant]
        counts = Counter(result.status for result in cases)
        lines.append(
            f"| {adapter.variant} | {adapter.execution} | {len(cases)} / {planned} | "
            f"{counts['passed']} | {counts['failed']} | {counts['timeout']} | "
            f"{counts['error']} | {counts['cancelled']} |"
        )
    lines.extend(
        [
            "",
            "## Aggregated metrics",
            "",
            "Means describe the scored cases only; the coverage column exposes missing results. "
            "Fixture mode labels do not represent measured retrieval algorithms.",
            "",
            "| Variant | Metric | Mean | Scored / planned |",
            "|---|---|---:|---:|",
        ]
    )
    for adapter in manifest.adapters:
        cases = [result for result in results if result.adapter.variant == adapter.variant]
        for name, metric in _metrics(cases, planned).items():
            lines.append(
                f"| {adapter.variant} | {name} | {_display(metric['mean'])} | "
                f"{metric['scored']} / {metric['planned']} |"
            )
    lines.extend(
        [
            "",
            "## Latency",
            "",
            "| Variant | Mean duration ms | Recorded / planned |",
            "|---|---:|---:|",
        ]
    )
    for adapter in manifest.adapters:
        cases = [result for result in results if result.adapter.variant == adapter.variant]
        duration = mean(result.duration_ms for result in cases) if cases else None
        lines.append(f"| {adapter.variant} | {_display(duration)} | {len(cases)} / {planned} |")
    lines.extend(
        [
            "",
            "## Case results",
            "",
            "| Variant | Case | Repeat | Outcome | Duration ms | Checks |",
            "|---|---|---:|---|---:|---|",
        ]
    )
    for result in results:
        checks = (
            ", ".join(f"{key}={value}" for key, value in result.score.checks.items())
            if result.score
            else "unknown"
        )
        lines.append(
            f"| {result.adapter.variant} | {result.case_id} | {result.repeat} | "
            f"{result.status} | {result.duration_ms} | "
            f"{_cell(checks)} |"
        )
    lines.extend(
        [
            "",
            "Aggregates and measured usage: `summary.json`. "
            "Per-case records: `cases.jsonl`. Reproduction metadata: `manifest.json`.",
            "",
        ]
    )
    with (destination / "evaluation_report.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
