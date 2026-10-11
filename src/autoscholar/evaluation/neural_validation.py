"""Bounded true-neural retrieval validation, independent of paid/provider ability."""

import argparse
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import uuid4

from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import BenchmarkSuite, CaseResult, RunConfiguration, RunManifest
from autoscholar.evaluation.neural_client import DockerNeuralClient
from autoscholar.evaluation.neural_retrieval import neural_adapters
from autoscholar.evaluation.pack import inspect_run
from autoscholar.evaluation.rag_adapter import MODES, RAGInputs, RAGLabels
from autoscholar.evaluation.retrieval_ablation import RetrievalFixture
from autoscholar.evaluation.runner import _git_state, _write_json, run_evaluation

# Targets are fixed before inference, never adjusted after seeing results. Sparse/BM25 is
# measured but is NOT promised to provide multilingual semantic understanding.
TARGET_MODES = ("dense", "hybrid", "hybrid_rerank")
TARGETS = {"all": 0.85, "en": 0.75, "zh": 0.75, "cross": 0.75}


def validate_corpus(suite: BenchmarkSuite, fixture: RetrievalFixture) -> None:
    if suite.category != "rag" or fixture.suite_id != suite.id:
        raise ValueError("Neural suite/corpus category or identity differs")
    chunks = {item.id: item.document_id for item in fixture.chunks}
    query_ids = set()
    for case in suite.cases:
        query = RAGInputs.model_validate(case.inputs)
        labels = RAGLabels.model_validate(case.expected)
        if (
            query.query_id != case.id
            or fixture.queries.get(query.query_id) != case.prompt
            or case.id.split("-", 1)[0] not in ("en", "zh", "cross")
            or not set(labels.relevant_chunk_ids) <= chunks.keys()
            or not set(labels.relevant_document_ids) <= set(chunks.values())
            or not {chunks[key] for key in labels.relevant_chunk_ids}
            <= set(labels.relevant_document_ids)
        ):
            raise ValueError("Neural independent labels/query bindings are invalid")
        query_ids.add(query.query_id)
    if query_ids != set(fixture.queries):
        raise ValueError("Neural corpus contains unscheduled queries")
    if any(
        not any(case.id.startswith(prefix + "-") for case in suite.cases)
        for prefix in ("en", "zh", "cross")
    ):
        raise ValueError("All language subsets must be planned")


def semantic_summary(
    suite: BenchmarkSuite,
    manifest: RunManifest,
    results: list[CaseResult],
    *,
    source_unchanged: bool,
    cache_unchanged: bool | None,
    cleanup_verified: bool,
) -> dict[str, Any]:
    planned = len(suite.cases) * manifest.configuration.repeats
    seen = {(case.adapter.variant, case.case_id, case.repeat) for case in results}
    structural = (
        manifest.state == "completed"
        and len(manifest.adapters) == 4
        and [adapter.variant for adapter in manifest.adapters] == list(MODES)
        and len(seen) == len(results) == planned * 4
        and all(case.status == "passed" for case in results)
        and all(
            case.usage.external_api_calls == case.usage.total_tokens == 0
            and case.score is not None
            and case.score.task_success is None
            for case in results
        )
    )
    groups = []
    quality = structural
    for mode in MODES:
        for bucket, threshold in TARGETS.items():
            ids = {
                case.id
                for case in suite.cases
                if bucket == "all" or case.id.startswith(bucket + "-")
            }
            scheduled = len(ids) * manifest.configuration.repeats
            cases = [
                case for case in results if case.adapter.variant == mode and case.case_id in ids
            ]
            metrics = {}
            for name in (
                "chunk_hit_rate_at_5",
                "chunk_recall_at_5",
                "chunk_mrr",
                "chunk_ndcg_at_5",
                "document_recall_at_5",
                "document_mrr",
            ):
                numbers = [
                    value
                    for case in cases
                    if case.score and (value := case.score.metrics.get(name)) is not None
                ]
                metrics[name] = {
                    "mean": mean(numbers) if numbers else None,
                    "scored": len(numbers),
                    "planned": scheduled,
                }
            hit = metrics["chunk_hit_rate_at_5"]
            target = threshold if mode in TARGET_MODES else None
            met = (
                (
                    scheduled > 0
                    and hit["scored"] == scheduled
                    and hit["mean"] is not None
                    and hit["mean"] >= threshold
                )
                if target is not None
                else None
            )
            if target is not None:
                quality = quality and bool(met)
            groups.append(
                {
                    "mode": mode,
                    "language": bucket,
                    "metrics": metrics,
                    "minimum_hit_at_5": target,
                    "target_met": met,
                }
            )
    baseline = {
        (case.case_id, case.repeat, case.seed): case
        for case in results
        if case.adapter.variant == "hybrid"
    }
    pairs = []
    for case in results:
        if case.adapter.variant != "hybrid_rerank":
            continue
        reference = baseline.get((case.case_id, case.repeat, case.seed))
        if (
            reference
            and reference.score
            and case.score
            and (
                case.input_sha256 == reference.input_sha256
                and case.expected_sha256 == reference.expected_sha256
                and case.adapter.resources == reference.adapter.resources
            )
        ):
            pairs.append((reference.score, case.score))
    deltas = {}
    for metric in ("chunk_mrr", "chunk_recall_at_5", "chunk_ndcg_at_5"):
        differences = [
            after_value - before_value
            for before, after in pairs
            if (after_value := after.metrics.get(metric)) is not None
            and (before_value := before.metrics.get(metric)) is not None
        ]
        deltas[metric] = {
            "mean_delta": mean(differences) if differences else None,
            "paired": len(differences),
            "planned": planned,
        }
    engineering = (
        structural
        and len(pairs) == planned
        and source_unchanged
        and cache_unchanged is True
        and cleanup_verified
    )
    return {
        "schema_version": 1,
        "run_id": manifest.run_id,
        "git_revision": manifest.git_revision,
        "git_dirty": manifest.git_dirty,
        "state": manifest.state,
        "planned": planned * 4,
        "recorded": len(results),
        "engineering_acceptance": engineering,
        "semantic_quality_acceptance": engineering and quality,
        "phase10_overall_acceptance": False,
        "unverified": ["real_provider_model_ability"]
        + ([] if engineering and quality else ["bounded_default_neural_retrieval_quality"]),
        "source_unchanged": source_unchanged,
        "cache_unchanged": cache_unchanged,
        "owned_inference_container_cleaned": cleanup_verified,
        "quality_targets_frozen": TARGETS,
        "groups": groups,
        "reranker_vs_same_pool_hybrid": {
            "delta_direction": "hybrid_rerank_minus_hybrid",
            "scored_pairs": len(pairs),
            "planned": planned,
            "metrics": deltas,
        },
        "scope": "Self-authored public pre-chunked corpus; actual default cached neural models. "
        "No PDF ingestion, generation/faithfulness, general semantic guarantee, real provider "
        "ability, reranking superiority or statistical significance is claimed.",
    }


def write_semantic_report(directory: Path, summary: dict[str, Any]) -> None:
    _write_json(directory / "semantic_acceptance.json", summary)
    with (directory / "evaluation_report.md").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Actual cached neural semantic validation\n\n")
        stream.write(
            f"Engineering acceptance: `{summary['engineering_acceptance']}`; "
            f"bounded semantic quality acceptance: `{summary['semantic_quality_acceptance']}`. "
            "Overall Phase 10/provider ability remains unverified.\n\n"
        )
        stream.write(summary["scope"] + "\n\n")
        stream.write(
            "Models infer in a cache-only CPU container with no network, credentials or Docker "
            "socket. Model snapshots/source are read-only, actual files are hashed and checked "
            "again after execution. Fresh local SQLite/Qdrant uses public text only.\n\n"
            "Relevance is measured, not an engineering prerequisite. Targets were frozen before "
            "inference; all planned records remain in coverage. Sparse/BM25 has no multilingual "
            "quality target. Errors/missing metrics cannot pass a quality target. Repeated fixed "
            "queries are NOT independent model samples. Latency excludes initial model loading, "
            "readiness inference, corpus embedding/indexing and shutdown.\n\n"
            "Both hybrid modes use the SAME ten ordered candidates before returning five results. "
            "Paired deltas are descriptive, not proof of reranking benefit. Vendor calls/tokens "
            "are zero; local inference calls are separate metrics and monetary cost is unknown.\n\n"
            "| Mode | Language | Hit@5 | MRR | Scored / planned | Frozen minimum Hit@5 | Met |\n"
            "|---|---|---:|---:|---:|---:|---|\n"
        )
        for group in summary["groups"]:
            hit = group["metrics"]["chunk_hit_rate_at_5"]
            mrr = group["metrics"]["chunk_mrr"]
            stream.write(
                f"| {group['mode']} | {group['language']} | {hit['mean']} | {mrr['mean']} | "
                f"{hit['scored']} / {hit['planned']} | {group['minimum_hit_at_5']} | "
                f"{group['target_met']} |\n"
            )
        stream.write(
            "\nAudit: `semantic_acceptance.json` and `inference_identity.json`. "
            "Primary records and suite/resource/Git identities remain in the standard "
            "manifest/JSONL. Application settings and private files are not loaded.\n"
        )


async def run_neural(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    from autoscholar.evaluation.__main__ import PreparedEvaluation

    root = (repo_root / "data/evaluation").resolve()
    output = args.output_root.resolve() if args.output_root else root
    workspace = (root / "neural-workspaces").resolve()
    if (
        not root.is_relative_to(repo_root.resolve())
        or not output.is_relative_to(root)
        or not workspace.is_relative_to(root)
    ):
        raise ValueError("Neural output escaped data/evaluation")
    revision, dirty = _git_state(repo_root)
    if revision is None or dirty is not False:
        raise ValueError("Actual acceptance requires clean committed source")
    configuration = RunConfiguration(
        profile="injected",
        seed=args.seed,
        repeats=args.repeats,
        case_timeout_seconds=args.timeout,
    )
    suite, digest = load_suite(repo_root / "benchmarks/semantic/public_v1.json")
    fixture, fixture_digest = load_fixture(
        repo_root / "benchmarks/semantic/corpus_v1.json", RetrievalFixture
    )
    validate_corpus(suite, fixture)
    client = DockerNeuralClient()
    group_root = output / ("neural-" + uuid4().hex)
    directory = None
    cache_unchanged = None
    summary: dict[str, Any] | None = None
    prepared: PreparedEvaluation | None = None
    try:
        identity = await client.start(
            source=str((repo_root / "src").resolve()),
            image=args.image,
            cache_volume=args.cache_volume,
        )
        if _git_state(repo_root) != (revision, False):
            raise ValueError("Source changed during neural model preflight")
        print(
            "Actual cached neural models ready; CPU/network-none, no provider requests", flush=True
        )
        async with neural_adapters(
            fixture, fixture_sha256=fixture_digest, client=client, workspace_root=workspace
        ) as adapters:
            prepared = PreparedEvaluation(suite, digest, list(adapters), configuration, group_root)
            directory = await run_evaluation(
                suite,
                dataset_sha256=digest,
                adapters=adapters,
                configuration=configuration,
                output_root=group_root,
                repo_root=repo_root,
            )
        try:
            verification = await client.call("verify", {})
            cache_unchanged = isinstance(verification, dict) and (
                verification.get("cache_and_code_unchanged") is True
            )
        except ValueError:
            cache_unchanged = False
    finally:
        await client.close()
        if directory is None and group_root.is_dir():
            runs = list(group_root.iterdir())
            if len(runs) == 1:
                directory = runs[0]
        if directory is not None:
            assert prepared is not None
            evidence = inspect_run(prepared, directory, revision=revision)
            manifest, results = evidence.manifest, evidence.results
            summary = semantic_summary(
                suite,
                manifest,
                results,
                source_unchanged=_git_state(repo_root) == (revision, False),
                cache_unchanged=cache_unchanged,
                cleanup_verified=client.cleanup_verified,
            )
            _write_json(
                directory / "inference_identity.json",
                {
                    **identity.model_dump(mode="json"),
                    "inference_image": client.image_sha256,
                    "public_corpus_chunks": len(fixture.chunks),
                    "corpus_inference_calls": {"dense_documents": 1, "sparse_documents": 1},
                    "measured_inference_counts": client.counts,
                    "cleanup_verified": client.cleanup_verified,
                },
            )
            write_semantic_report(directory, summary)
    assert directory is not None and summary is not None
    return directory, bool(summary["semantic_quality_acceptance"])
