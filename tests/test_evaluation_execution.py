"""Scorer/protocol tests only; the separate integration entry point runs real Docker."""

import asyncio
import base64
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from autoscholar.coding.sandbox import (
    SandboxArtifact,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation.coding_adapter import (
    CodingFixture,
    CodingOracleFixture,
    InjectedCodingAdapter,
    score_coding,
)
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.experiment_adapter import (
    BoundedExperimentSpecification,
    CheckpointOracle,
    ExperimentFixture,
    InjectedExperimentAdapter,
    score_experiment,
)
from autoscholar.evaluation.isolated_components import (
    ObservedSandbox,
    local_component_task,
    oracle_json,
    sandbox_resources,
)
from autoscholar.evaluation.models import Observation
from autoscholar.evaluation.runner import EvaluationAdapter
from autoscholar.experiment.artifacts import ArtifactError, ArtifactManager

ROOT = Path(__file__).resolve().parents[1]
CODING = ROOT / "benchmarks/coding"
EXPERIMENT = ROOT / "benchmarks/experiment"


def collected(payload: dict[str, Any]) -> SandboxRunResult:
    raw = json.dumps(payload).encode()
    return SandboxRunResult(
        status="succeeded",
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
        artifacts=[
            SandboxArtifact(
                path="outputs/eval_oracle.json",
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                data_base64=base64.b64encode(raw).decode(),
            )
        ],
    )


class ReplaySandbox:
    """Explicit predetermined outputs for unit control-flow assertions, not real execution."""

    def __init__(self, results: list[SandboxRunResult] | None = None) -> None:
        self.results = list(results or [])
        self.requests: list[SandboxRunRequest] = []

    async def health(self) -> SandboxHealth:
        return SandboxHealth(
            status="ok",
            engine=True,
            image=True,
            image_sha256="a" * 64,
            mnist_dataset=True,
            dataset_id="mnist",
            dataset_sha256="b" * 64,
        )

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.requests.append(request)
        return self.results.pop(0)

    async def close(self) -> None:
        pass


def coding_observation() -> Observation:
    return Observation(
        payload={
            "status": "accepted",
            "error_code": None,
            "compile_passed": True,
            "tests_passed": True,
            "agent_completed": True,
            "repairs": 0,
            "sandbox_runs": 3,
            "oracle": {"schema_version": 1, "passed_count": 4, "total_count": 4},
        }
    )


def experiment_observation() -> Observation:
    return Observation(
        payload={
            "status": "accepted",
            "error_code": None,
            "service_completed": True,
            "metric_extraction_valid": True,
            "artifact_set_valid": True,
            "artifact_integrity_valid": True,
            "artifact_count": 10,
            "verified_artifact_count": 10,
            "sandbox_runs": 4,
            "oracle": {
                "schema_version": 1,
                "dataset_verified": True,
                "plots_verified": True,
                "models": [
                    {"model": "mlp", "accuracy": 0.5, "parameters": 101770, "verified": True},
                    {"model": "cnn", "accuracy": 0.6, "parameters": 9098, "verified": True},
                ],
            },
        }
    )


@pytest.mark.parametrize(
    "defect",
    ["oracle_missing", "oracle_failed", "static_failed", "pytest_failed", "agent_incomplete"],
)
def test_coding_status_alone_is_not_independent_acceptance(defect: str) -> None:
    observation = coding_observation()
    payload = observation.payload
    if defect == "oracle_missing":
        payload["oracle"] = None
    elif defect == "oracle_failed":
        oracle = payload["oracle"]
        assert isinstance(oracle, dict)
        oracle["passed_count"] = 1
    else:
        payload[
            {
                "static_failed": "compile_passed",
                "pytest_failed": "tests_passed",
                "agent_incomplete": "agent_completed",
            }[defect]
        ] = False
    score = score_coding(
        observation,
        {
            "status": "accepted",
            "compile_passed": True,
            "tests_passed": True,
            "repairs": 0,
        },
    )
    assert not all(score.checks.values())
    assert score.metrics["coding_acceptance"] == 0.0
    assert score.task_success is None


def test_missing_coding_oracle_is_not_a_successfully_detected_deception() -> None:
    actual = coding_observation()
    actual.payload.update(
        {"status": "oracle_rejected", "error_code": "oracle_mismatch", "oracle": None}
    )
    score = score_coding(
        actual,
        {
            "status": "oracle_rejected",
            "error_code": "oracle_mismatch",
            "compile_passed": True,
            "tests_passed": True,
            "repairs": 0,
        },
    )
    assert not score.checks["deceptive_self_tests_detected"]
    assert score.metrics["functional_correctness"] is None


@pytest.mark.parametrize(
    "defect", ["checkpoint", "dataset", "plot", "artifact", "metrics", "service"]
)
def test_successful_experiment_record_does_not_hide_independent_failures(defect: str) -> None:
    actual = experiment_observation()
    oracle = actual.payload["oracle"]
    assert isinstance(oracle, dict)
    if defect == "checkpoint":
        models = oracle["models"]
        assert isinstance(models, list) and isinstance(models[0], dict)
        models[0]["verified"] = False
    elif defect in ("dataset", "plot"):
        oracle["dataset_verified" if defect == "dataset" else "plots_verified"] = False
    else:
        actual.payload[
            {
                "artifact": "artifact_integrity_valid",
                "metrics": "metric_extraction_valid",
                "service": "service_completed",
            }[defect]
        ] = False
    score = score_experiment(actual, {"status": "accepted", "artifact_count": 10})
    assert not all(score.checks.values())
    assert score.metrics["experiment_acceptance"] == 0.0
    assert score.task_success is None


def test_checkpoint_oracle_cannot_double_count_one_model() -> None:
    payload = experiment_observation().payload["oracle"]
    assert isinstance(payload, dict) and isinstance(payload["models"], list)
    payload["models"][1] = deepcopy(payload["models"][0])
    with pytest.raises(ValidationError, match="exactly once"):
        CheckpointOracle.model_validate(payload)


def test_missing_experiment_oracle_is_not_a_detected_checkpoint_mismatch() -> None:
    actual = experiment_observation()
    actual.payload.update(
        {
            "status": "oracle_rejected",
            "error_code": "checkpoint_mismatch",
            "oracle": None,
        }
    )
    score = score_experiment(
        actual,
        {
            "status": "oracle_rejected",
            "error_code": "checkpoint_mismatch",
            "artifact_count": 10,
        },
    )
    assert not score.checks["independent_failure_detected"]
    assert score.metrics["checkpoint_correctness"] is None


@pytest.mark.parametrize(
    "configuration",
    [
        {"epochs": 3},
        {"train_samples": 513},
        {"test_samples": 1024},
        {"seed": True},
        {"schema_version": True},
        {"learning_rate": float("inf")},
    ],
)
def test_expensive_or_coercive_experiment_inputs_are_rejected(
    configuration: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        BoundedExperimentSpecification.model_validate(configuration)


@pytest.mark.parametrize("path", ["../escape.py", "D:/outside.py", "/tmp/escape.py"])
def test_coding_source_paths_cannot_escape_before_execution(path: str) -> None:
    with pytest.raises(ValidationError):
        CodingFixture.model_validate(
            {
                "schema_version": 1,
                "suite_id": "suite",
                "scripts": {
                    "case": {
                        "prompt": "Public",
                        "initial_files": {"solution.py": "", path: "print(1)"},
                    }
                },
            }
        )


@pytest.mark.parametrize(
    "defect", ["digest", "size", "path", "missing", "failed", "duplicate", "truncated"]
)
def test_oracle_artifacts_are_verified_not_accepted_from_stdout(defect: str) -> None:
    result = collected({"schema_version": 1, "passed_count": 4, "total_count": 4})
    result.stdout = 'Every test passed. {"passed_count":999}'
    if defect == "digest":
        result.artifacts[0].sha256 = "0" * 64
    elif defect == "size":
        result.artifacts[0].size_bytes += 1
    elif defect == "path":
        result.artifacts[0].path = "../private.json"
    elif defect == "missing":
        result.artifacts = []
    elif defect == "failed":
        result.status = "failed"
    elif defect == "duplicate":
        result.artifacts.append(result.artifacts[0])
    else:
        result.truncated = True
    with pytest.raises(ValueError):
        oracle_json(result)


async def test_coding_repairs_real_workspace_but_does_not_show_oracle_to_author(
    tmp_path: Path,
) -> None:
    ok = SandboxRunResult(status="succeeded", exit_code=0, stdout="", stderr="", duration_ms=1)
    bad = SandboxRunResult(
        status="failed", exit_code=1, stdout="", stderr="SyntaxError", duration_ms=1
    )
    sandbox = ReplaySandbox(
        [bad, ok, ok, collected({"schema_version": 1, "passed_count": 4, "total_count": 4})]
    )
    fixture, digest = load_fixture(CODING / "source_fixture_v1.json", CodingFixture)
    oracle, oracle_digest = load_fixture(CODING / "oracle_fixture_v1.json", CodingOracleFixture)
    suite, _ = load_suite(CODING / "public_v1.json")
    case = next(case for case in suite.cases if case.id == "syntax-repair")
    adapter = InjectedCodingAdapter(
        fixture,
        oracle,
        fixture_sha256=digest,
        oracle_sha256=oracle_digest,
        sandbox=sandbox,
        resources=sandbox_resources(await sandbox.health(), dataset=False),
        workspace_root=tmp_path,
    )
    adapter.validate_case(case)
    observed = await adapter.execute(case.prompt, case.inputs, seed=42)
    assert all(adapter.score(observed, case.expected).checks.values())
    assert sandbox.requests[0].files["solution.py"] != sandbox.requests[1].files["solution.py"]
    assert [request.action for request in sandbox.requests] == [
        "static_check",
        "static_check",
        "run_pytest",
        "run_python",
    ]
    assert all("oracle_cases.json" not in request.files for request in sandbox.requests[:-1])
    assert "oracle_cases.json" in sandbox.requests[-1].files
    assert observed.usage.external_api_calls == 0


async def test_actual_saved_file_tampering_is_detected(tmp_path: Path) -> None:
    async with local_component_task(tmp_path, "Public artifact test", mode="experiment") as (
        task_id,
        store,
        workspace,
    ):
        workspace.initialize(task_id)
        record = await store.create_experiment(task_id=task_id, name="public", specification={})
        manager = ArtifactManager(workspace, store)
        artifact = await manager.save(
            task_id, record.id, "outputs/metrics.json", b'{"public":true}'
        )
        assert manager.read_verified(task_id, artifact) == b'{"public":true}'
        workspace.write_bytes(
            task_id, "metrics.json", b'{"public":false}', area="outputs", overwrite=True
        )
        with pytest.raises(ArtifactError, match="changed"):
            manager.read_verified(task_id, artifact)


async def test_changed_sandbox_resources_fail_closed() -> None:
    observed = ObservedSandbox(ReplaySandbox(), {"sandbox_image": "c" * 64})
    with pytest.raises(ValueError, match="changed"):
        await observed.health()


@pytest.mark.parametrize(
    "container", ["autoscholar-api-1", "normal-worker", "--privileged", "name;cmd"]
)
def test_production_or_shell_like_container_targets_are_rejected(container: str) -> None:
    with pytest.raises(ValueError, match="dedicated"):
        DockerEvaluationSandbox(container)


async def test_remote_docker_context_is_rejected_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = DockerEvaluationSandbox("autoscholar-eval-public-sandbox-manager-1")
    requests: list[tuple[str, ...]] = []

    async def metadata(*arguments: str) -> bytes:
        requests.append(arguments)
        return b"remote" if arguments == ("context", "show") else b'"tcp://remote.invalid:2376"'

    monkeypatch.setattr(sandbox, "_metadata", metadata)
    with pytest.raises(ValueError, match="local Docker"):
        await sandbox.health()
    assert not sandbox.verified and len(requests) == 2


@pytest.mark.parametrize("category", ["coding", "experiment"])
def test_cli_requires_explicit_isolated_execution_not_host_fallback(
    category: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation",
            "run",
            "--category",
            category,
            "--dataset",
            str((CODING if category == "coding" else EXPERIMENT) / "public_v1.json"),
        ],
    )
    assert cli.main() == 2
    assert not (tmp_path / "data/evaluation").exists()


async def test_all_fifteen_public_execution_cases_validate_without_running_sources(
    tmp_path: Path,
) -> None:
    coding, coding_digest = load_fixture(CODING / "source_fixture_v1.json", CodingFixture)
    oracle, oracle_digest = load_fixture(CODING / "oracle_fixture_v1.json", CodingOracleFixture)
    experiment, experiment_digest = load_fixture(
        EXPERIMENT / "experiment_fixture_v1.json", ExperimentFixture
    )
    sandbox = ReplaySandbox()
    adapters: list[EvaluationAdapter] = [
        InjectedCodingAdapter(
            coding,
            oracle,
            fixture_sha256=coding_digest,
            oracle_sha256=oracle_digest,
            sandbox=sandbox,
            resources=sandbox_resources(await sandbox.health(), dataset=False),
            workspace_root=tmp_path,
        ),
        InjectedExperimentAdapter(
            experiment,
            fixture_sha256=experiment_digest,
            sandbox=sandbox,
            resources=sandbox_resources(await sandbox.health(), dataset=True),
            workspace_root=tmp_path,
        ),
    ]
    total = 0
    for adapter, directory in zip(adapters, (CODING, EXPERIMENT), strict=True):
        suite, _ = load_suite(directory / "public_v1.json")
        for case in suite.cases:
            adapter.validate_case(case)
            total += 1
    assert total == 15 and sandbox.requests == []


async def test_cancelled_isolated_case_disposes_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    original = AsyncEngine.dispose
    disposed = 0
    entered = asyncio.Event()

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        nonlocal disposed
        disposed += 1
        await original(self, close=close)

    async def run() -> None:
        async with local_component_task(tmp_path, "Public cancellation", mode="coding"):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert disposed == 1
