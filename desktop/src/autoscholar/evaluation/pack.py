"""Fixed, serial engineering acceptance pack; no settings, providers or auto-retries."""

import argparse
import asyncio
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict
from uuid import uuid4

from pydantic import Field

from autoscholar.evaluation.ablation_report import comparison
from autoscholar.evaluation.datasets import (
    MAX_DATASET_BYTES,
    decode_json,
    payload_digest,
    read_bounded,
)
from autoscholar.evaluation.models import (
    AdapterIdentity,
    CaseResult,
    Category,
    EvaluationModel,
    Identifier,
    MeasuredUsage,
    RunConfiguration,
    RunManifest,
)
from autoscholar.evaluation.report import RunSummary, summarize
from autoscholar.evaluation.runner import _git_state, _write_json

if TYPE_CHECKING:
    from autoscholar.evaluation.__main__ import PreparedEvaluation


@dataclass(frozen=True)
class PackJob:
    id: str
    category: Category
    profile: Literal["offline", "injected"]
    ablations: bool = False


JOBS = (
    PackJob("rag_replay", "rag", "offline"),
    PackJob("research", "research", "offline"),
    PackJob("tool", "tool", "offline"),
    PackJob("coding", "coding", "injected"),
    PackJob("experiment", "experiment", "injected"),
    PackJob("end_to_end", "end_to_end", "injected"),
    PackJob("workflow_ablations", "end_to_end", "injected", True),
    PackJob("retrieval_ablations", "rag", "injected", True),
)
SCOPE = {
    "rag_replay": "Fixed rankings: scorer/replay only, NOT actual retrieval.",
    "research": "Actual Research Agent; fixed public provider/search decisions.",
    "tool": "Actual Calculator; fixed tool selection, NOT model selection ability.",
    "coding": "Actual isolated Coding/pytest and independent held-out oracle.",
    "experiment": "Actual CPU MNIST subset training and independent checkpoint oracle.",
    "end_to_end": "Actual durable workflow; scripted decisions, independent artifact grading.",
    "workflow_ablations": "Actual controlled execution paths; NOT planning/review intelligence.",
    "retrieval_ablations": "Actual local lexical Qdrant/RRF/Jaccard; NOT neural semantic quality.",
}


class PackSlot(EvaluationModel):
    id: Identifier
    category: Category
    suite_id: Identifier
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration: RunConfiguration
    adapters: list[AdapterIdentity] = Field(min_length=1, max_length=10)
    cases_per_variant: int = Field(ge=1, le=10000)
    state: Literal["pending", "running", "recorded", "error", "cancelled", "invalid"] = "pending"
    run: str | None = Field(default=None, pattern=r"^runs/[a-z_]+/eval-[a-f0-9]{32}$")
    # No exception text, prompts, private paths or generated answers in the pack manifest.
    reason: Literal["execution_error", "cancelled", "invalid_evidence"] | None = None


class PackManifest(EvaluationModel):
    schema_version: Literal[1] = 1
    pack_id: str = Field(pattern=r"^pack-[a-f0-9]{32}$")
    started_at: str
    completed_at: str | None = None
    git_revision: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    git_dirty: Literal[False] = False
    source_unchanged: bool = True
    state: Literal["running", "completed", "error", "cancelled"] = "running"
    slots: list[PackSlot] = Field(min_length=8, max_length=8)


@dataclass
class RunEvidence:
    manifest: RunManifest
    results: list[CaseResult]
    digests: dict[str, str]


class UsageStatistics(TypedDict):
    total: int | float | None
    measured_records: int
    planned: int


class SlotSummary(TypedDict):
    id: str
    category: Category
    scope: str
    state: str
    run: str | None
    planned: int
    recorded: int
    passed: int
    engineering_checks_passed: bool
    primary_sha256: dict[str, str] | None
    summary: RunSummary | None
    ablations: dict[str, Any] | None


class PackSummary(TypedDict):
    schema_version: int
    pack_id: str
    state: str
    git_revision: str
    source_unchanged: bool
    engineering_acceptance: bool
    phase10_overall_acceptance: bool
    unverified: list[str]
    planned: int
    recorded: int
    passed: int
    no_external_api_measured: bool
    usage: dict[str, UsageStatistics]
    slots: list[SlotSummary]


def _file(directory: Path, name: str) -> Path:
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise ValueError("Evidence file escaped its run directory")
    return path


def inspect_run(prepared: "PreparedEvaluation", directory: Path, *, revision: str) -> RunEvidence:
    """Recompute from bounded primary records, never trust summary/report sidecars."""
    if (
        directory.is_symlink()
        or directory.resolve().parent != prepared.destination.resolve()
        or not re.fullmatch(r"eval-[a-f0-9]{32}", directory.name)
    ):
        raise ValueError("Run escaped its assigned slot")
    raw_manifest = read_bounded(_file(directory, "manifest.json"))
    with _file(directory, "cases.jsonl").open("rb") as stream:
        raw_cases = stream.read(MAX_DATASET_BYTES + 1)
    if len(raw_cases) > MAX_DATASET_BYTES:
        raise ValueError("Case evidence exceeds 8 MiB")
    manifest = RunManifest.model_validate(decode_json(raw_manifest))
    if (
        manifest.run_id != directory.name
        or manifest.suite_id != prepared.suite.id
        or manifest.dataset_sha256 != prepared.digest
        or manifest.configuration != prepared.configuration
        or manifest.adapters != [adapter.identity for adapter in prepared.adapters]
        or manifest.git_revision != revision
        or manifest.git_dirty is not False
        or manifest.state == "running"
        or manifest.completed_at is None
    ):
        raise ValueError("Manifest does not match this committed pack")
    lines = raw_cases.splitlines()
    planned = len(prepared.suite.cases) * prepared.configuration.repeats * len(prepared.adapters)
    if len(lines) > planned:
        raise ValueError("Too many case records")
    results = [CaseResult.model_validate(decode_json(line)) for line in lines]
    cases = {case.id: case for case in prepared.suite.cases}
    identities = {adapter.identity.variant: adapter.identity for adapter in prepared.adapters}
    seen: set[tuple[str, str, int]] = set()
    for result in results:
        case = cases.get(result.case_id)
        key = (result.adapter.variant, result.case_id, result.repeat)
        if (
            key in seen
            or case is None
            or result.adapter != identities.get(result.adapter.variant)
            or result.category != prepared.suite.category
            or result.repeat > prepared.configuration.repeats
            or result.seed != (prepared.configuration.seed + result.repeat - 1) % (2**32)
            or result.input_sha256 != payload_digest({"prompt": case.prompt, "inputs": case.inputs})
            or result.expected_sha256 != payload_digest(case.expected)
        ):
            raise ValueError("Invalid or duplicate scheduled case binding")
        seen.add(key)
        score = result.score
        if result.status in ("passed", "failed"):
            if (
                score is None
                or (result.status == "passed") != all(score.checks.values())
                or result.reason != (None if result.status == "passed" else "checks_failed")
            ):
                raise ValueError("Outcome does not match its checks")
        elif (
            score is not None
            or result.status == "skipped"
            or result.reason
            != {"error": "adapter_error", "timeout": "timeout", "cancelled": "cancelled"}.get(
                result.status
            )
        ):
            raise ValueError("Unscored outcome has invalid reason")
        if score and score.task_success is not None and result.category != "end_to_end":
            raise ValueError("Only E2E may claim task success")
        if score and result.category == "end_to_end" and score.task_success is None:
            raise ValueError("E2E completion must be scored explicitly")
    return RunEvidence(
        manifest,
        results,
        {
            "manifest": hashlib.sha256(raw_manifest).hexdigest(),
            "cases": hashlib.sha256(raw_cases).hexdigest(),
        },
    )


def _usage(results: Sequence[CaseResult], planned: int) -> dict[str, UsageStatistics]:
    usage: dict[str, UsageStatistics] = {}
    for name in MeasuredUsage.model_fields:
        values = [getattr(result.usage, name) for result in results]
        known = [value for value in values if value is not None]
        usage[name] = {
            "total": sum(known) if known and len(known) == planned else None,
            "measured_records": len(known),
            "planned": planned,
        }
    return usage


def pack_summary(manifest: PackManifest, evidence: dict[str, RunEvidence]) -> PackSummary:
    slots: list[SlotSummary] = []
    all_results: list[CaseResult] = []
    planned_total = 0
    verified = True
    no_api = True
    for job, slot in zip(JOBS, manifest.slots, strict=True):
        planned = slot.cases_per_variant * len(slot.adapters)
        planned_total += planned
        run = evidence.get(slot.id)
        results = run.results if run else []
        all_results.extend(results)
        passed = sum(result.status == "passed" for result in results)
        paired = (
            comparison(results, planned=slot.cases_per_variant)
            if job.ablations and results
            else None
        )
        complete_pairs = not job.ablations or bool(
            paired
            and len(paired["contrasts"]) == len(slot.adapters) - 1
            and all(
                item["matched"] == item["scored_pairs"] == slot.cases_per_variant
                for item in paired["contrasts"]
            )
        )
        accepted = bool(
            run
            and slot.state == "recorded"
            and run.manifest.state == "completed"
            and len(results) == passed == planned
            and complete_pairs
        )
        verified = verified and accepted
        no_api = (
            no_api
            and len(results) == planned
            and all(
                all(
                    getattr(result.usage, field) == 0
                    for field in (
                        "external_api_calls",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    )
                )
                for result in results
            )
        )
        slots.append(
            {
                "id": slot.id,
                "category": slot.category,
                "scope": SCOPE[slot.id],
                "state": slot.state,
                "run": slot.run,
                "planned": planned,
                "recorded": len(results),
                "passed": passed,
                "engineering_checks_passed": accepted,
                "primary_sha256": run.digests if run else None,
                "summary": summarize(run.manifest, results, slot.cases_per_variant)
                if run
                else None,
                "ablations": paired,
            }
        )
    accepted = manifest.state == "completed" and manifest.source_unchanged and verified and no_api
    return {
        "schema_version": 1,
        "pack_id": manifest.pack_id,
        "state": manifest.state,
        "git_revision": manifest.git_revision,
        "source_unchanged": manifest.source_unchanged,
        "engineering_acceptance": accepted,
        "phase10_overall_acceptance": False,
        "unverified": ["production_neural_semantic_retrieval", "real_provider_model_ability"],
        "planned": planned_total,
        "recorded": len(all_results),
        "passed": sum(result.status == "passed" for result in all_results),
        "no_external_api_measured": no_api,
        "usage": _usage(all_results, planned_total),
        "slots": slots,
    }


def write_pack_report(
    directory: Path, manifest: PackManifest, evidence: dict[str, RunEvidence]
) -> bool:
    summary = pack_summary(manifest, evidence)
    _write_json(directory / "summary.json", summary)
    accepted = bool(summary["engineering_acceptance"])
    lines = [
        "# AutoScholar-Eval engineering acceptance pack",
        "",
        f"Pack: `{manifest.pack_id}`; state: `{manifest.state}`",
        f"Code: `{manifest.git_revision}`; source unchanged: `{manifest.source_unchanged}`",
        f"Engineering acceptance: **{'PASS' if accepted else 'NOT ACCEPTED'}**.",
        "Overall Phase 10 acceptance: **NOT VERIFIED**. Production neural semantic retrieval "
        "and real provider/model ability remain separate unverified work.",
        "",
        "## Interpretation",
        "",
        "Only fixed public engineering fixtures and actual explicitly injected local components "
        "run. No application Settings/.env, provider requests, host-code fallback or auto-retries.",
        "Checks passing is NOT task completion. Expected refusals/fabricated accuracy negatives "
        "can pass checks without completing the task. E2E rates stay per suite/variant; "
        "there is no mixed overall task-success rate.",
        "All eight scheduled suites stay in denominators even after errors or cancellation. "
        "Primary manifest/cases bindings are validated; sidecar summaries are not trusted. "
        "Different revisions, dirty source, duplicated cases and mismatched resources cannot pass.",
        "No statistical significance, ranking superiority, neural semantic or real LLM ability "
        "is inferred. Repeated fixtures are not independent model samples. Latency excludes "
        "environment setup and report writing. Synthetic budget units are not vendor tokens.",
        "Unknown usage/cost is not zero; no API fee estimate is inferred. Runtime files "
        "remain local under ignored data/evaluation; do not publish private files or reports.",
        "",
        "## Coverage",
        "",
        "| Suite | State | Recorded / planned | Check passes | Report |",
        "|---|---|---:|---:|---|",
    ]
    for slot, item in zip(manifest.slots, summary["slots"], strict=True):
        link = f"[report]({slot.run}/evaluation_report.md)" if slot.run else "not started"
        lines.append(
            f"| {slot.id} | {slot.state} | {item['recorded']} / {item['planned']} | "
            f"{item['passed']} | {link} |"
        )
    lines.extend(["", "## Component metrics (separate denominators)", ""])
    for slot in manifest.slots:
        lines.extend([f"### {slot.id}", "", SCOPE[slot.id], ""])
        run = evidence.get(slot.id)
        if not run:
            lines.extend(["No validated evidence; all scheduled records remain unverified.", ""])
            continue
        groups = summarize(run.manifest, run.results, slot.cases_per_variant)["groups"]
        lines.extend(
            [
                "| Variant | Check-pass rate | E2E task-success rate | Mean duration ms |",
                "|---|---:|---:|---:|",
            ]
        )
        for group in groups:
            lines.append(
                f"| {group['variant']} | {group['check_pass_rate']:.6g} | "
                f"{_display(group['task_success_rate'])} | "
                f"{_display(group['mean_duration_ms'])} |"
            )
        lines.extend(["", "| Variant | Metric | Mean | Scored / planned |", "|---|---|---:|---:|"])
        for group in groups:
            for name, metric in group["metrics"].items():
                lines.append(
                    f"| {group['variant']} | {name} | {_display(metric['mean'])} | "
                    f"{metric['scored']} / {metric['planned']} |"
                )
        lines.append("")
        if slot.id.endswith("ablations"):
            lines.extend(
                [
                    "Delta = variant minus baseline; "
                    "paired cases/inputs/gold/resources must match.",
                    "",
                    "| Variant | Scored / planned pairs | Task-completion delta | Callback delta |",
                    "|---|---:|---:|---:|",
                ]
            )
            for contrast in comparison(run.results, planned=slot.cases_per_variant)["contrasts"]:
                task = contrast["metrics"].get("task_completion", {}).get("mean_delta")
                lines.append(
                    f"| {contrast['variant']} | {contrast['scored_pairs']} / "
                    f"{contrast['planned']} | "
                    f"{_display(task)} | "
                    f"{_display(contrast['usage']['model_calls']['mean_delta'])} |"
                )
            lines.append("")
    lines.extend(
        ["## Measured usage", "", "| Field | Total | Measured / planned |", "|---|---:|---:|"]
    )
    for name, usage in summary["usage"].items():
        lines.append(
            f"| {name} | {_display(usage['total'])} | "
            f"{usage['measured_records']} / {usage['planned']} |"
        )
    lines.extend(
        ["", "Audit: `pack_manifest.json`, `summary.json` and each run's primary records.", ""]
    )
    temporary = directory / "evaluation_report.md.tmp"
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
    temporary.replace(directory / "evaluation_report.md")
    return accepted


def _display(value: int | float | None) -> str:
    return "unknown" if value is None else f"{value:.6g}"


async def run_pack(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    from autoscholar.evaluation import __main__ as cli

    root = (repo_root / "data/evaluation").resolve()
    output = args.output_root.resolve() if args.output_root else root
    if not root.is_relative_to(repo_root.resolve()) or not output.is_relative_to(root):
        raise ValueError("Pack output escaped data/evaluation")
    revision, dirty = _git_state(repo_root)
    if revision is None or dirty is not False:
        raise ValueError("Acceptance requires clean committed source")
    directory = output / ("pack-" + uuid4().hex)
    prepared_runs = []
    slots = []
    # Complete preflight BEFORE writing/starting a pack; injection is explicit, no fallback.
    for job in JOBS:
        namespace = argparse.Namespace(
            category=job.category,
            ablations=job.ablations,
            dataset=None,
            fixture=None,
            oracle=None,
            profile=job.profile,
            sandbox_container=args.sandbox_container
            if job.category in ("coding", "experiment", "end_to_end")
            else None,
            modes=None,
            ks=None,
            seed=args.seed,
            repeats=args.repeats,
            timeout=args.timeout,
            output_root=directory / "runs" / job.id,
        )
        prepared = await cli._prepare(namespace)
        for adapter in prepared.adapters:
            for case in prepared.suite.cases:
                adapter.validate_case(case.model_copy(deep=True))
        prepared_runs.append(prepared)
        slots.append(
            PackSlot(
                id=job.id,
                category=job.category,
                suite_id=prepared.suite.id,
                dataset_sha256=prepared.digest,
                configuration=prepared.configuration,
                adapters=[adapter.identity for adapter in prepared.adapters],
                cases_per_variant=len(prepared.suite.cases) * prepared.configuration.repeats,
            )
        )
    if _git_state(repo_root) != (revision, False):
        raise ValueError("Source changed during preflight")
    # All Docker suites must be on the same pinned image/dataset throughout the pack.
    for resource in ("sandbox_image", "dataset"):
        digests = {
            adapter.resources[resource]
            for slot in slots
            for adapter in slot.adapters
            if resource in adapter.resources
        }
        if len(digests) > 1:
            raise ValueError("Sandbox resources changed during preflight")
    directory.mkdir(parents=True, exist_ok=False)
    manifest = PackManifest(
        pack_id=directory.name,
        started_at=datetime.now(UTC).isoformat(),
        git_revision=revision,
        slots=slots,
    )
    evidence: dict[str, RunEvidence] = {}
    _write_json(directory / "pack_manifest.json", manifest.model_dump(mode="json"))
    write_pack_report(directory, manifest, evidence)
    try:
        for job, slot, prepared in zip(JOBS, slots, prepared_runs, strict=True):
            slot.state = "running"
            _write_json(directory / "pack_manifest.json", manifest.model_dump(mode="json"))
            print(f"Pack {job.id}: starting {slot.cases_per_variant * len(slot.adapters)} records")
            try:
                run = await cli._evaluate(prepared, ablations=job.ablations)
                slot.run = run.relative_to(directory).as_posix()
                evidence[slot.id] = inspect_run(prepared, run, revision=revision)
                slot.state = "recorded"
            except asyncio.CancelledError:
                slot.state, slot.reason = "cancelled", "cancelled"
                raise
            except Exception:
                slot.state, slot.reason = "error", "execution_error"
                # Stop on infrastructure/IO failure; never silently retry or rerun a whole suite.
                manifest.state = "error"
                break
            finally:
                if slot.id not in evidence and prepared.destination.is_dir():
                    runs = list(prepared.destination.iterdir())
                    if len(runs) == 1:
                        try:
                            partial = runs[0]
                            evidence[slot.id] = inspect_run(prepared, partial, revision=revision)
                            slot.run = partial.relative_to(directory).as_posix()
                        except (OSError, ValueError):
                            slot.state, slot.reason = "invalid", "invalid_evidence"
                _write_json(directory / "pack_manifest.json", manifest.model_dump(mode="json"))
                write_pack_report(directory, manifest, evidence)
        else:
            manifest.state = "completed"
    except asyncio.CancelledError:
        manifest.state = "cancelled"
        raise
    except Exception:
        manifest.state = "error"
        raise
    finally:
        manifest.completed_at = datetime.now(UTC).isoformat()
        manifest.source_unchanged = _git_state(repo_root) == (revision, False)
        # Reports remain local mutable files, not signed attestations. Verify the exact
        # primary bytes again at completion so late edits cannot retain acceptance.
        for slot, prepared in zip(slots, prepared_runs, strict=True):
            if slot.run and (previous := evidence.get(slot.id)):
                try:
                    current = inspect_run(prepared, directory / slot.run, revision=revision)
                    if current.digests != previous.digests:
                        raise ValueError("Primary evidence changed after execution")
                except (OSError, ValueError):
                    slot.state, slot.reason = "invalid", "invalid_evidence"
                    evidence.pop(slot.id, None)
        _write_json(directory / "pack_manifest.json", manifest.model_dump(mode="json"))
        accepted = write_pack_report(directory, manifest, evidence)
    return directory, accepted
