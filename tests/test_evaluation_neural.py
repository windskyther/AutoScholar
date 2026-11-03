"""Neural evaluation contracts with explicit fake inference; NOT real-model results."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.core.config import Settings
from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation import neural_client as client_module
from autoscholar.evaluation import neural_validation as validation_module
from autoscholar.evaluation import neural_worker as worker_module
from autoscholar.evaluation.component_fixture import fixture_usage, load_fixture
from autoscholar.evaluation.datasets import load_suite, payload_digest
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkSuite,
    CaseResult,
    RunConfiguration,
    RunManifest,
    ScoreCard,
)
from autoscholar.evaluation.neural_client import DockerNeuralClient, NeuralIdentity
from autoscholar.evaluation.neural_retrieval import (
    DenseVectors,
    RemoteDense,
    RemoteReranker,
    RemoteSparse,
    SparseVector,
    neural_adapters,
)
from autoscholar.evaluation.neural_validation import (
    semantic_summary,
    validate_corpus,
    write_semantic_report,
)
from autoscholar.evaluation.neural_worker import (
    DENSE_MODEL,
    RERANKER_MODEL,
    SPARSE_MODEL,
    Method,
    NeuralEngine,
    NeuralRequest,
    RerankRequest,
    cached_snapshot,
    inference_code_digest,
)
from autoscholar.evaluation.rag_adapter import MODES
from autoscholar.evaluation.retrieval_ablation import RetrievalFixture
from autoscholar.rag import embeddings as embeddings_module
from autoscholar.rag.models import RetrievedChunk

ROOT = cli.REPO_ROOT
REVISION = "a" * 40


def corpus() -> tuple[BenchmarkSuite, RetrievalFixture, str]:
    suite, _ = load_suite(ROOT / "benchmarks/semantic/public_v1.json")
    fixture, digest = load_fixture(ROOT / "benchmarks/semantic/corpus_v1.json", RetrievalFixture)
    return suite, fixture, digest


class FakeInference(DockerNeuralClient):
    """Intentionally tied/poor rankings, so engine checks must not become quality claims."""

    def __init__(self) -> None:
        super().__init__()
        self.identity = NeuralIdentity(
            resources={
                "dense_model": "0" * 64,
                "sparse_model": "1" * 64,
                "reranker_model": "2" * 64,
                "inference_code": inference_code_digest(),
            },
            models=[DENSE_MODEL, SPARSE_MODEL, RERANKER_MODEL],
            snapshot_revisions={key: "a" * 40 for key in worker_module.MODEL_REPOSITORIES},
            fastembed="test-stub",
            onnxruntime="test-stub",
            cpu_only=True,
            local_files_only=True,
            setup_inference_calls={"dense_query": 1, "sparse_query": 1, "rerank": 1},
            isolation_verified=True,
        )
        self.image_sha256 = "3" * 64
        self.payloads: list[tuple[Method, dict[str, object]]] = []
        self.cancel_method: Method | None = None
        self.cache_changed = False

    async def start(self, *, source: str, image: str, cache_volume: str) -> NeuralIdentity:
        assert self.identity is not None
        return self.identity

    async def call(self, method: Method, payload: dict[str, object]) -> object:
        self.payloads.append((method, payload))
        if method == self.cancel_method:
            raise asyncio.CancelledError()
        if method == "verify":
            return {"cache_and_code_unchanged": not self.cache_changed}
        self.counts[method] = self.counts.get(method, 0) + 1
        if method == "dense_query":
            return [1.0] + [0.0] * 383
        if method == "dense_documents":
            texts = payload["texts"]
            assert isinstance(texts, list)
            return [[1.0] + [0.0] * 383 for _ in texts]
        if method == "sparse_query":
            return {"indices": [42], "values": [1.0]}
        if method == "sparse_documents":
            texts = payload["texts"]
            assert isinstance(texts, list)
            return [{"indices": [42], "values": [1.0]} for _ in texts]
        if method == "rerank":
            chunks, limit = payload["chunks"], payload["limit"]
            assert isinstance(chunks, list) and isinstance(limit, int)
            return [{"id": chunk["id"], "score": 0.0} for chunk in chunks[:limit]]
        raise ValueError("Unsupported explicit contract-stub method")

    async def close(self) -> None:
        self.closed = self.cleanup_verified = True


def chunk() -> RetrievedChunk:
    return RetrievedChunk(
        id="public",
        project_id="project",
        document_id="document",
        title="Public",
        page=1,
        section=None,
        content="Public inference test",
        score=0.0,
        ordinal=0,
    )


def synthetic_results() -> tuple[BenchmarkSuite, RunManifest, list[CaseResult]]:
    suite, _, _ = corpus()
    identities = [
        AdapterIdentity(
            name="explicit-contract-stub",
            category="rag",
            variant=mode,
            execution="injected",
            resources={"test_input": "0" * 64},
        )
        for mode in MODES
    ]
    manifest = RunManifest(
        run_id="eval-" + "0" * 32,
        started_at="2026-10-12T00:00:00+00:00",
        completed_at="2026-10-12T00:01:00+00:00",
        suite_id=suite.id,
        dataset_sha256="0" * 64,
        configuration=RunConfiguration(profile="injected"),
        adapters=identities,
        git_revision=REVISION,
        git_dirty=False,
        python_version="test",
        platform="test",
        state="completed",
    )
    results = [
        CaseResult(
            case_id=case.id,
            category="rag",
            adapter=identity,
            repeat=1,
            seed=42,
            status="passed",
            duration_ms=1.0,
            usage=fixture_usage(model_calls=0),
            score=ScoreCard(
                checks={"contract": True},
                metrics={
                    "chunk_hit_rate_at_5": 1.0,
                    "chunk_recall_at_5": 1.0,
                    "chunk_mrr": 1.0,
                    "chunk_ndcg_at_5": 1.0,
                    "document_recall_at_5": 1.0,
                    "document_mrr": 1.0,
                },
            ),
            input_sha256=payload_digest({"prompt": case.prompt, "inputs": case.inputs}),
            expected_sha256=payload_digest(case.expected),
        )
        for identity in identities
        for case in suite.cases
    ]
    return suite, manifest, results


def summarize_results(
    suite: BenchmarkSuite, manifest: RunManifest, results: list[CaseResult], **kwargs: Any
) -> dict[str, Any]:
    return semantic_summary(
        suite,
        manifest,
        results,
        **{
            "source_unchanged": True,
            "cache_unchanged": True,
            "cleanup_verified": True,
            **kwargs,
        },
    )


def test_public_neural_corpus_has_independent_language_subsets() -> None:
    suite, fixture, _ = corpus()
    validate_corpus(suite, fixture)
    assert len(suite.cases) == len(fixture.chunks) == 20
    assert [
        sum(case.id.startswith(prefix + "-") for case in suite.cases)
        for prefix in ("en", "zh", "cross")
    ] == [8, 8, 4]
    assert "rankings" not in fixture.model_dump()
    assert "relevant_chunk_ids" not in json.dumps(fixture.model_dump())


@pytest.mark.parametrize("mutation", ["prompt", "missing_query", "unknown_gold", "documents"])
def test_corpus_binding_rejects_mismatch(mutation: str) -> None:
    suite, fixture, _ = corpus()
    if mutation == "prompt":
        suite.cases[0].prompt = "Different public prompt"
    elif mutation == "missing_query":
        fixture.queries.pop(suite.cases[0].id)
    elif mutation == "unknown_gold":
        suite.cases[0].expected["relevant_chunk_ids"] = ["unknown"]
    else:
        suite.cases[0].expected["relevant_document_ids"] = ["different"]
    with pytest.raises(ValueError):
        validate_corpus(suite, fixture)


async def test_actual_qdrant_and_service_paths_use_input_only_stub_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No Settings, external HTTP or generation in neural evaluation")

    monkeypatch.setattr(Settings, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    suite, fixture, digest = corpus()
    client = FakeInference()
    async with neural_adapters(
        fixture, fixture_sha256=digest, client=client, workspace_root=tmp_path
    ) as adapters:
        for adapter in adapters:
            case = suite.cases[0]
            adapter.validate_case(case)
            observation = await adapter.execute(case.prompt, case.inputs, seed=42)
            score = adapter.score(observation, case.expected)
            assert all(score.checks.values())
            assert score.task_success is None
            assert observation.usage.external_api_calls == 0
        assert adapters[-1].pair_audit
        with pytest.raises(ValueError, match="changed"):
            await adapters[0].execute("Modified public question", suite.cases[0].inputs, seed=42)
    assert client.counts["dense_documents"] == client.counts["sparse_documents"] == 1
    assert client.counts["dense_query"] == client.counts["sparse_query"] == 3
    assert client.counts["rerank"] == 1
    assert "relevant_chunk_ids" not in json.dumps(client.payloads)


async def test_reranker_missing_hybrid_pair_cannot_claim_paired_pool(tmp_path: Path) -> None:
    suite, fixture, digest = corpus()
    client = FakeInference()
    async with neural_adapters(
        fixture, fixture_sha256=digest, client=client, workspace_root=tmp_path
    ) as adapters:
        case = suite.cases[0]
        observation = await adapters[-1].execute(case.prompt, case.inputs, seed=42)
        score = adapters[-1].score(observation, case.expected)
        assert not score.checks["paired_hybrid_pool"]


@pytest.mark.parametrize(
    "payload",
    [
        {"vectors": [[1.0]]},
        {"vectors": [[float("nan")] * 384]},
        {"vectors": [[float("inf")] * 384]},
    ],
)
def test_invalid_dense_shape_or_nonfinite_values_fail(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DenseVectors.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"indices": [1, 1], "values": [1.0, 2.0]},
        {"indices": [1], "values": []},
        {"indices": [-1], "values": [1.0]},
    ],
)
def test_sparse_shape_is_validated(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SparseVector.model_validate(payload)


async def test_wrappers_validate_rank_ids_and_document_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeInference()

    async def wrong(method: Method, payload: dict[str, object]) -> object:
        return [{"id": "invented", "score": 0.0}]

    monkeypatch.setattr(client, "call", wrong)
    with pytest.raises(ValueError, match="invented"):
        await RemoteReranker(client).rerank("Public", [chunk()], limit=1)
    with pytest.raises(ValueError):
        await RemoteDense(client).embed_documents(["Public"])
    with pytest.raises(ValueError):
        await RemoteSparse(client).embed_documents(["Public", "Other"])


def test_json_rerank_request_preserves_strict_dataclass_fields() -> None:
    from dataclasses import asdict

    request = RerankRequest.model_validate_json(
        json.dumps({"query": "Public", "chunks": [asdict(chunk())], "limit": 1})
    )
    assert request.chunks[0] == chunk()


def test_quality_targets_are_separate_from_engineering_and_provider_acceptance(
    tmp_path: Path,
) -> None:
    suite, manifest, results = synthetic_results()
    summary = summarize_results(suite, manifest, results)
    assert summary["engineering_acceptance"] and summary["semantic_quality_acceptance"]
    assert summary["phase10_overall_acceptance"] is False
    assert summary["unverified"] == ["real_provider_model_ability"]
    assert summary["reranker_vs_same_pool_hybrid"]["scored_pairs"] == 20
    (tmp_path / "evaluation_report.md").write_text("# Explicit contract-stub report\n")
    write_semantic_report(tmp_path, summary)
    report = (tmp_path / "evaluation_report.md").read_text()
    assert "no network" in report and "Sparse/BM25 has no multilingual" in report
    assert (tmp_path / "semantic_acceptance.json").is_file()


def test_poor_neural_relevance_is_not_hidden_as_engineering_error() -> None:
    suite, manifest, results = synthetic_results()
    for case in results:
        assert case.score is not None
        if case.adapter.variant == "dense":
            case.score.metrics["chunk_hit_rate_at_5"] = 0.0
    summary = summarize_results(suite, manifest, results)
    assert summary["engineering_acceptance"] is True
    assert summary["semantic_quality_acceptance"] is False
    assert "bounded_default_neural_retrieval_quality" in summary["unverified"]


def test_sparse_multilingual_low_scores_are_measured_without_fabricating_target() -> None:
    suite, manifest, results = synthetic_results()
    for case in results:
        assert case.score is not None
        if case.adapter.variant == "sparse":
            case.score.metrics["chunk_hit_rate_at_5"] = 0.0
    summary = summarize_results(suite, manifest, results)
    assert summary["semantic_quality_acceptance"]
    assert all(
        group["target_met"] is None for group in summary["groups"] if group["mode"] == "sparse"
    )


def test_unknown_or_missing_quality_scores_cannot_pass() -> None:
    suite, manifest, results = synthetic_results()
    assert results[0].score is not None
    results[0].score.metrics["chunk_hit_rate_at_5"] = None
    summary = summarize_results(suite, manifest, results)
    assert not summary["semantic_quality_acceptance"]
    group = summary["groups"][0]
    assert group["metrics"]["chunk_hit_rate_at_5"] == {"mean": 1.0, "scored": 19, "planned": 20}
    summary = summarize_results(suite, manifest, results[:-1])
    assert summary["planned"] == 80 and summary["recorded"] == 79
    assert not summary["engineering_acceptance"]


@pytest.mark.parametrize(
    "condition",
    [
        {"source_unchanged": False},
        {"cache_unchanged": False},
        {"cache_unchanged": None},
        {"cleanup_verified": False},
    ],
)
def test_source_cache_or_cleanup_unknown_prevents_acceptance(condition: dict[str, object]) -> None:
    suite, manifest, results = synthetic_results()
    assert not summarize_results(suite, manifest, results, **condition)["engineering_acceptance"]


def test_mismatched_resources_cannot_make_reranker_pair_or_pass() -> None:
    suite, manifest, results = synthetic_results()
    results[-1].adapter = results[-1].adapter.model_copy(
        update={"resources": {"test_input": "f" * 64}}
    )
    summary = summarize_results(suite, manifest, results)
    assert summary["reranker_vs_same_pool_hybrid"]["scored_pairs"] == 19
    assert not summary["engineering_acceptance"]


def test_cached_snapshot_pins_actual_bytes_and_refuses_invalid_reference(tmp_path: Path) -> None:
    model = tmp_path / "models--public--model"
    snapshot = model / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (model / "refs").mkdir()
    reference = model / "refs/main"
    reference.write_text("a" * 40)
    (snapshot / "weights.onnx").write_bytes(b"explicit-test-bytes-not-real-model")
    _, first = cached_snapshot(tmp_path, "public/model")
    (snapshot / "weights.onnx").write_bytes(b"changed-explicit-test-bytes")
    _, second = cached_snapshot(tmp_path, "public/model")
    assert first != second
    reference.write_text("../escape")
    with pytest.raises(ValueError, match="revision"):
        cached_snapshot(tmp_path, "public/model")


async def test_worker_refuses_credentials_before_loading_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "ensure_isolation", lambda: None)
    monkeypatch.setenv("LLM_API_KEY", "private-never-export")
    with pytest.raises(ValueError, match="credentials"):
        await NeuralEngine().initialize()


async def test_worker_requires_handshake_and_bounded_payload() -> None:
    with pytest.raises(ValueError, match="initialized"):
        await NeuralEngine().dispatch(NeuralRequest(request_id=1, method="dense_query", payload={}))
    with pytest.raises(ValueError):
        NeuralRequest(request_id=0, method="identity")
    with pytest.raises(ValueError):
        RerankRequest.model_validate_json(json.dumps({"query": "x", "chunks": [], "limit": 0}))


@pytest.mark.parametrize("kind", ["dense", "sparse", "reranker"])
async def test_production_providers_receive_explicit_cache_only_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    calls = []
    sentinel = object()

    def constructor(**kwargs: object) -> object:
        calls.append(kwargs)
        return sentinel

    common = {
        "model": "public-test-model",
        "cache_dir": tmp_path,
        "local_files_only": True,
        "model_path": tmp_path / "snapshot",
        "threads": 2,
    }
    result: object
    if kind == "dense":
        monkeypatch.setattr(embeddings_module, "TextEmbedding", constructor)
        provider = embeddings_module.FastEmbedProvider(dimensions=384, **common)  # type: ignore[arg-type]
        result = await provider._get_embedding()
    elif kind == "sparse":
        monkeypatch.setattr(embeddings_module, "SparseTextEmbedding", constructor)
        sparse = embeddings_module.FastEmbedSparseProvider(**common)  # type: ignore[arg-type]
        result = await sparse._get_embedding()
    else:
        monkeypatch.setattr(embeddings_module, "TextCrossEncoder", constructor)
        reranker = embeddings_module.FastEmbedReranker(**common)  # type: ignore[arg-type]
        result = await reranker._get_reranker()
    assert result is sentinel
    assert calls == [
        {
            "model_name": "public-test-model",
            "cache_dir": str(tmp_path),
            "lazy_load": True,
            "local_files_only": True,
            "specific_model_path": str(tmp_path / "snapshot"),
            "threads": 2,
        }
    ]


async def test_neural_cli_harness_reports_poor_quality_and_partial_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ROOT / "benchmarks/semantic"
    target = tmp_path / "benchmarks/semantic"
    target.mkdir(parents=True)
    for filename in ("public_v1.json", "corpus_v1.json"):
        (target / filename).write_bytes((source / filename).read_bytes())
    monkeypatch.setattr(validation_module, "_git_state", lambda _: (REVISION, False))
    from autoscholar.evaluation import runner

    monkeypatch.setattr(runner, "_git_state", lambda _: (REVISION, False))
    client = FakeInference()
    monkeypatch.setattr(validation_module, "DockerNeuralClient", lambda: client)
    args = argparse.Namespace(
        image="explicit-contract-stub",
        cache_volume="explicit-contract-stub",
        output_root=None,
        repeats=1,
        seed=42,
        timeout=30,
    )
    directory, accepted = await validation_module.run_neural(args, repo_root=tmp_path)
    summary = json.loads((directory / "semantic_acceptance.json").read_text())
    identity = json.loads((directory / "inference_identity.json").read_text())
    assert not accepted and summary["engineering_acceptance"]
    assert summary["recorded"] == summary["planned"] == 80
    assert identity["fastembed"] == "test-stub" and identity["cleanup_verified"]
    assert client.cleanup_verified

    cancelled = FakeInference()
    cancelled.cancel_method = "dense_query"
    monkeypatch.setattr(validation_module, "DockerNeuralClient", lambda: cancelled)
    with pytest.raises(asyncio.CancelledError):
        await validation_module.run_neural(args, repo_root=tmp_path)
    summaries = [
        json.loads(path.read_text())
        for path in (tmp_path / "data/evaluation").rglob("semantic_acceptance.json")
    ]
    partial = next(item for item in summaries if item["state"] == "cancelled")
    assert partial["recorded"] == 1 and partial["planned"] == 80
    assert partial["cache_unchanged"] is None
    assert partial["owned_inference_container_cleaned"] and not partial["engineering_acceptance"]


@pytest.mark.parametrize("fault", ["foreign_id", "error", "negative_count", "empty", "cancel"])
async def test_worker_protocol_failure_closes_stream_without_retry(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = {"request_id": 1, "ok": True, "data": [1.0], "counts": {}}
    if fault == "foreign_id":
        request["request_id"] = 2
    elif fault == "error":
        request.update(ok=False, error="inference_failed")
    elif fault == "negative_count":
        request["counts"] = {"dense_query": -1}

    class Pipe:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, raw: bytes) -> None:
            self.writes.append(raw)

        async def drain(self) -> None:
            pass

        async def readline(self) -> bytes:
            if fault == "cancel":
                raise asyncio.CancelledError()
            return b"" if fault == "empty" else json.dumps(request).encode() + b"\n"

    class Process:
        stdin = stdout = Pipe()

    client = DockerNeuralClient()
    client.process = Process()  # type: ignore[assignment]
    cleanups = []

    async def close() -> None:
        cleanups.append(True)
        client.closed = True

    monkeypatch.setattr(client, "close", close)
    error = asyncio.CancelledError if fault == "cancel" else ValueError
    with pytest.raises(error):
        await client.call("dense_query", {"query": "Public"})
    assert cleanups == [True] and len(Process.stdin.writes) == 1
    with pytest.raises(ValueError, match="No active"):
        await client.call("dense_query", {"query": "Public"})


@pytest.mark.parametrize("fault", [None, "bad_models", "bad_snapshots", "bad_calls", "external"])
async def test_docker_start_pins_cached_image_and_limits_mounts(
    fault: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = []
    identity = FakeInference().identity
    assert identity is not None
    raw_identity = identity.model_dump(mode="json")
    if fault == "bad_models":
        raw_identity["models"] = ["wrong"] * 3
    elif fault == "bad_snapshots":
        raw_identity["snapshot_revisions"] = {}
    elif fault == "bad_calls":
        raw_identity["setup_inference_calls"] = {}

    async def metadata(*args: str) -> bytes:
        if args == ("context", "show"):
            return b"local-eval"
        if "context" in args and "inspect" in args:
            return json.dumps(
                "tcp://remote:2375" if fault == "external" else "unix:///socket"
            ).encode()
        if "image" in args:
            return b"sha256:" + b"3" * 64
        return b"[]" if "volume" in args else b""

    class Process:
        returncode = 0
        stdin = None

    async def spawn(*args: str, **kwargs: object) -> Process:
        commands.append(args)
        return Process()

    client = DockerNeuralClient()

    async def handshake(method: Method, payload: dict[str, object]) -> object:
        assert method == "identity" and not payload
        return raw_identity

    monkeypatch.setattr(client_module, "metadata", metadata)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(client, "call", handshake)
    if fault:
        with pytest.raises(ValueError):
            await client.start(
                source=str(tmp_path), image="public:cached", cache_volume="public_cache"
            )
        assert not commands if fault == "external" else client.cleanup_verified
        return
    await client.start(source=str(tmp_path), image="public:cached", cache_volume="public_cache")
    await client.close()
    command = commands[0]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--user") + 1] == "65532:65532"
    assert "--read-only" in command and "--cap-drop" in command
    assert "sha256:" + "3" * 64 in command
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert len(mounts) == 2 and all(value.endswith(",readonly") for value in mounts)
    assert all(
        secret not in " ".join(command) for secret in ("API_KEY", ".env", "docker.sock", "设计")
    )
    assert client.cleanup_verified
