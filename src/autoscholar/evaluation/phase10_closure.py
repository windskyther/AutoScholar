"""Read-only historical audits and a separately scoped Phase 10 closure report."""

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import JsonValue

from autoscholar.evaluation.__main__ import PreparedEvaluation
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import load_suite, read_bounded
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    Observation,
    RunManifest,
    ScoreCard,
)
from autoscholar.evaluation.neural_validation import semantic_summary, validate_corpus
from autoscholar.evaluation.pack import JOBS, PackManifest, inspect_run, pack_summary
from autoscholar.evaluation.provider_chinese import inspect_chinese
from autoscholar.evaluation.provider_closure import load_provider_closure
from autoscholar.evaluation.provider_prior import read_public
from autoscholar.evaluation.retrieval_ablation import RetrievalFixture
from autoscholar.evaluation.runner import _git_state, _write_json


@dataclass
class EvidenceOnlyAdapter:
    identity: AdapterIdentity

    def validate_case(self, case: BenchmarkCase) -> None:
        raise AssertionError("Historical closure must not run an adapter")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        raise AssertionError("Historical closure must not execute code or call APIs")

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        raise AssertionError("Use primary case records, not a new runtime sample")


def inspect_engineering(
    directory: Path, *, repo_root: Path
) -> tuple[dict[str, Any], dict[str, str]]:
    root = (repo_root / "data/evaluation").resolve()
    directory = directory.resolve()
    if directory.parent != root or not re.fullmatch(r"pack-[a-f0-9]{32}", directory.name):
        raise ValueError("Select an exact historical engineering pack")
    raw, digest = read_public(directory / "pack_manifest.json", root)
    manifest = PackManifest.model_validate(raw)
    if manifest.pack_id != directory.name or [slot.id for slot in manifest.slots] != [
        job.id for job in JOBS
    ]:
        raise ValueError("Engineering pack identity/order differs")
    evidence, digests = {}, {"pack_manifest.json": digest}
    for job, slot in zip(JOBS, manifest.slots, strict=True):
        if not slot.run or (slot.category, slot.configuration.profile) != (
            job.category,
            job.profile,
        ):
            raise ValueError("Missing or mismatched engineering suite")
        path = (directory / slot.run).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Engineering subrun escaped its pack")
        dataset = repo_root / (
            "benchmarks/ablations/"
            + ("workflow" if job.category == "end_to_end" else "retrieval")
            + "_v1.json"
            if job.ablations
            else f"benchmarks/{job.category}/public_v1.json"
        )
        suite, suite_digest = load_suite(dataset)
        if (
            suite.id != slot.suite_id
            or suite_digest != slot.dataset_sha256
            or len(suite.cases) * slot.configuration.repeats != slot.cases_per_variant
        ):
            raise ValueError("Engineering dataset differs from recorded public inputs")
        prepared = PreparedEvaluation(
            suite,
            suite_digest,
            [EvidenceOnlyAdapter(identity) for identity in slot.adapters],
            slot.configuration,
            path.parent,
        )
        run = inspect_run(prepared, path, revision=manifest.git_revision)
        evidence[slot.id] = run
        for name, sha in run.digests.items():
            digests[slot.run + "/" + ("manifest.json" if name == "manifest" else "cases.jsonl")] = (
                sha
            )
    return dict(pack_summary(manifest, evidence)), digests


def inspect_semantic(directory: Path, *, repo_root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    root = (repo_root / "data/evaluation").resolve()
    directory = directory.resolve()
    if (
        not directory.is_relative_to(root)
        or not re.fullmatch(r"eval-[a-f0-9]{32}", directory.name)
        or not re.fullmatch(r"neural-[a-f0-9]{32}", directory.parent.name)
    ):
        raise ValueError("Select an exact historical neural run")
    raw, _ = read_public(directory / "manifest.json", root)
    manifest = RunManifest.model_validate(raw)
    if manifest.git_revision is None:
        raise ValueError("Missing historical neural source revision")
    suite, digest = load_suite(repo_root / "benchmarks/semantic/public_v1.json")
    fixture, _ = load_fixture(repo_root / "benchmarks/semantic/corpus_v1.json", RetrievalFixture)
    validate_corpus(suite, fixture)
    prepared = PreparedEvaluation(
        suite,
        digest,
        [EvidenceOnlyAdapter(identity) for identity in manifest.adapters],
        manifest.configuration,
        directory.parent,
    )
    primary = inspect_run(prepared, directory, revision=manifest.git_revision)
    recorded, record_sha = read_public(directory / "semantic_acceptance.json", root)
    identity, identity_sha = read_public(directory / "inference_identity.json", root)
    verified = all(
        identity.get(name) is True
        for name in ("cpu_only", "local_files_only", "isolation_verified", "cleanup_verified")
    )
    for adapter in manifest.adapters:
        if any(adapter.resources.get(key) != value for key, value in identity["resources"].items()):
            raise ValueError("Neural actual model/source identities differ from primary records")
    summary = semantic_summary(
        suite,
        manifest,
        primary.results,
        source_unchanged=recorded.get("source_unchanged") is True,
        cache_unchanged=recorded.get("cache_unchanged"),
        cleanup_verified=verified,
    )
    if summary != recorded:
        raise ValueError("Recorded neural sidecar disagrees with recomputed primary metrics")
    return summary, {
        "manifest.json": primary.digests["manifest"],
        "cases.jsonl": primary.digests["cases"],
        "semantic_acceptance.json": record_sha,
        "inference_identity.json": identity_sha,
    }


async def run_closure(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    revision, dirty = _git_state(repo_root)
    if not revision or dirty is not False:
        raise ValueError("Closure audit requires clean committed source")
    root = (repo_root / "data/evaluation").resolve()
    if not root.is_relative_to(repo_root.resolve()):
        raise ValueError("Closure output escaped evaluation root")
    engineering, engineering_digests = inspect_engineering(
        args.engineering_run, repo_root=repo_root
    )
    semantic, semantic_digests = inspect_semantic(args.semantic_run, repo_root=repo_root)
    proof = await load_provider_closure(
        repo_root=repo_root,
        original_run=args.original_run,
        independent_run=args.independent_run,
        tool_run=args.tool_run,
    )
    chinese: dict[str, Any] = (
        inspect_chinese(args.chinese_run, proof=proof)
        if args.chinese_run
        else {"status": "pending", "accepted": False, "attempts": []}
    )
    previous = proof.previous.records
    coverage = [row["status"] == "passed" for row in previous]
    coverage[1:3] = [row["status"] == "passed" for row in proof.tools]
    coverage[8] = chinese["accepted"]
    current_source_clean = _git_state(repo_root) == (revision, False)
    accepted = bool(
        engineering["engineering_acceptance"]
        and semantic["semantic_quality_acceptance"]
        and all(coverage)
        and proof.unchanged()
        and current_source_clean
    )
    attempts = proof.attempts + chinese["attempts"]
    known = [
        row["usage"]["total_tokens"]
        for row in attempts
        if row["provider"] == "llm" and row["usage"]["total_tokens"] is not None
    ]
    counts = {name: sum(row["provider"] == name for row in attempts) for name in ("llm", "tavily")}
    bindings = {}
    for directory, hashes in (
        (args.engineering_run.resolve(), engineering_digests),
        (args.semantic_run.resolve(), semantic_digests),
    ):
        for name, sha in hashes.items():
            bindings[(directory / name).relative_to(root).as_posix()] = sha
            if hashlib.sha256(read_bounded(directory / name)).hexdigest() != sha:
                raise ValueError("Historical primary evidence changed during closure")
    for name, sha in chinese.get("primary_sha256", {}).items():
        if hashlib.sha256(read_bounded(root / name)).hexdigest() != sha:
            raise ValueError("Chinese evidence changed during closure")
        bindings[name] = sha
    summary = {
        "schema_version": 1,
        "git_revision": revision,
        "source_unchanged": current_source_clean,
        "scope": "engineering_and_bounded_capabilities_v1",
        "bounded_phase10_closure_acceptance": accepted,
        "phase10_overall_acceptance": False,
        "main_merge_authorized": False,
        "unverified": ["autonomous_real_llm_end_to_end", "real_model_statistical_ablations"],
        "remaining_bounded_gaps": []
        if accepted
        else ["research_chinese_v2_confirmation"]
        if not chinese["accepted"]
        else ["inspect_component_failures"],
        "engineering": engineering,
        "semantic": semantic,
        "provider": {
            "covered": sum(coverage),
            "planned": 10,
            "original_frozen_passed": sum(row["status"] == "passed" for row in previous),
            "original_results": previous,
            "tool_confirmation": proof.tools,
            "chinese_v2": chinese,
            "old_results_unchanged": proof.unchanged(),
        },
        "history": proof.binding(),
        "primary_sha256": bindings,
        "external_requests_in_this_audit": 0,
        "cumulative_physical_requests": counts,
        "cumulative_requests_total": len(attempts),
        "cumulative_known_vendor_tokens": sum(known),
        "cumulative_vendor_tokens": sum(known) if len(known) == counts["llm"] else None,
        "monetary_cost": None,
    }
    directory = root / ("closure-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    _write_json(directory / "closure_acceptance.json", summary)
    (directory / "evaluation_report.md").write_text(
        "# Phase 10 traceable bounded closure\n\n"
        + f"Bounded closure: {accepted}. Overall Phase 10: NOT ACCEPTED. No main merge.\n\n"
        + "Historical engineering records and semantic metrics were revalidated, not rerun. "
        "V1 provider failures remain immutable. V2 uses a new prompt and the frozen grader. "
        "Component samples/reused records are not a fresh full-suite or autonomous E2E run.\n\n"
        + f"Engineering: {engineering['passed']}/{engineering['planned']}; "
        + f"semantic gate: {semantic['semantic_quality_acceptance']}; "
        + f"bounded capability coverage: {sum(coverage)}/10.\n\n"
        + f"Chinese v2: {chinese['status']}. "
        + f"Original v1 score: {summary['provider']['original_frozen_passed']}/10.\n\n"
        + f"Audit API requests: 0; cumulative physical requests: {len(attempts)} {counts}. "
        + f"Known tokens: {sum(known)}; total/money unknown if usage/price unavailable.\n\n"
        + "Unverified: autonomous real-LLM E2E and real-model statistical ablations. "
        "This report does not silently expand or approve those paid evaluations.\n",
        encoding="utf-8",
    )
    return directory, accepted
