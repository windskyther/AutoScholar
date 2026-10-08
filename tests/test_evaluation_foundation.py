import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from autoscholar.evaluation.datasets import decode_json, load_suite
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    BenchmarkSuite,
    MeasuredUsage,
    Observation,
    RunConfiguration,
    ScoreCard,
)
from autoscholar.evaluation.runner import run_evaluation


def suite() -> BenchmarkSuite:
    return BenchmarkSuite(
        schema_version=1,
        id="public-foundation",
        category="tool",
        description="Self-authored runner contract cases",
        provenance="Public test fixture",
        cases=[
            BenchmarkCase(
                id="first",
                prompt="public first",
                inputs={"marker": "public"},
                expected={"correct": True},
            ),
            BenchmarkCase(id="second", prompt="public second", expected={"correct": True}),
        ],
    )


class Adapter:
    identity = AdapterIdentity(
        name="contract-fixture", category="tool", variant="baseline", execution="fixture"
    )

    def __init__(self, action: str = "pass") -> None:
        self.action = action
        self.requests: list[dict[str, JsonValue]] = []

    def validate_case(self, case: BenchmarkCase) -> None:
        if "correct" not in case.expected:
            raise ValueError("Missing test oracle")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        self.requests.append(dict(inputs))
        inputs["marker"] = "mutated"
        if "first" in prompt:
            if self.action == "error":
                raise RuntimeError("secret-key-and-private-prompt")
            if self.action == "timeout":
                await asyncio.sleep(0.1)
            if self.action == "cancel":
                raise asyncio.CancelledError()
        return Observation(
            payload={"correct": self.action != "fail"},
            usage=MeasuredUsage(
                model_calls=0, external_api_calls=0, input_tokens=0, output_tokens=0, total_tokens=0
            ),
        )

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        correct = observation.payload["correct"] == expected["correct"]
        expected["correct"] = False
        return ScoreCard(checks={"correct": correct}, metrics={"accuracy": float(correct)})


def read_records(directory: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (directory / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    ]


async def run(tmp_path: Path, adapter: Adapter, **kwargs: object) -> Path:
    return await run_evaluation(
        suite(),
        dataset_sha256="a" * 64,
        adapters=[adapter],
        configuration=RunConfiguration.model_validate(kwargs),
        output_root=tmp_path / "runs",
        repo_root=Path(__file__).resolve().parents[1],
    )


def test_suite_loader_tracks_exact_utf8_digest(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    raw = suite().model_dump_json().encode("utf-8")
    path.write_bytes(raw)
    loaded, digest = load_suite(path)
    assert loaded == suite()
    assert digest == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "raw", [b'{"id":"a","id":"b"}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e309}', b"\xff", b"{"]
)
def test_json_loader_rejects_ambiguous_invalid_and_nonfinite_inputs(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode_json(raw)


def test_loader_is_bounded_and_nonempty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autoscholar.evaluation.datasets.MAX_DATASET_BYTES", 8)
    path = tmp_path / "suite.json"
    for raw in (b"", b"123456789"):
        path.write_bytes(raw)
        with pytest.raises(ValueError, match="nonempty"):
            load_suite(path)


def test_suite_rejects_duplicate_ids_unknown_fields_and_untyped_inputs() -> None:
    payload = suite().model_dump()
    payload["cases"] = [payload["cases"][0], payload["cases"][0]]
    with pytest.raises(ValidationError, match="unique"):
        BenchmarkSuite.model_validate(payload)
    payload = suite().model_dump()
    payload["credentials"] = "not allowed"
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(payload)
    payload = suite().model_dump()
    payload["cases"][0]["prompt"] = 123
    with pytest.raises(ValidationError):
        BenchmarkSuite.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [{"repeats": 0}, {"case_timeout_seconds": float("nan")}, {"profile": "live"}, {"seed": True}],
)
def test_run_configuration_requires_explicit_supported_bounded_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RunConfiguration.model_validate(payload)


def test_usage_does_not_conflate_budget_tokens_with_actual_usage() -> None:
    measured = MeasuredUsage(budget_tokens=120000)
    assert measured.total_tokens is None
    assert measured.monetary_cost is None
    with pytest.raises(ValidationError):
        MeasuredUsage(input_tokens=1, output_tokens=2, total_tokens=4)


async def test_runner_separates_labels_and_isolates_repeats(tmp_path: Path) -> None:
    adapter = Adapter()
    original = suite()
    destination = await run_evaluation(
        original,
        dataset_sha256="a" * 64,
        adapters=[adapter],
        configuration=RunConfiguration(repeats=2),
        output_root=tmp_path / "runs",
        repo_root=Path(__file__).resolve().parents[1],
    )
    assert all("correct" not in request for request in adapter.requests)
    assert adapter.requests[0] == adapter.requests[2] == {"marker": "public"}
    assert original.cases[0].inputs == {"marker": "public"}
    assert original.cases[0].expected == {"correct": True}
    records = read_records(destination)
    assert len(records) == 4 and all(record["status"] == "passed" for record in records)
    assert all("prompt" not in record and "expected" not in record for record in records)
    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    group = summary["groups"][0]
    assert group["check_pass_rate"] == 1.0
    assert group["task_success_rate"] is None
    assert group["usage"]["total_tokens"] == 0
    assert group["usage"]["monetary_cost"] is None
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "completed"
    assert len(manifest["git_revision"]) == 40
    assert manifest["configuration"]["repeats"] == 2


@pytest.mark.parametrize(("action", "status"), [("error", "error"), ("timeout", "timeout")])
async def test_errors_and_timeouts_are_recorded_then_remaining_cases_run(
    tmp_path: Path,
    action: str,
    status: str,
) -> None:
    destination = await run(tmp_path, Adapter(action), case_timeout_seconds=0.01)
    records = read_records(destination)
    assert [record["status"] for record in records] == [status, "passed"]
    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    group = summary["groups"][0]
    assert group["check_pass_rate"] == 0.5
    assert group["usage"]["total_tokens"] is None
    assert group["metrics"]["accuracy"] == {"mean": 1.0, "scored": 1, "planned": 2}
    assert "secret-key-and-private-prompt" not in "".join(
        path.read_text(encoding="utf-8") for path in destination.iterdir()
    )


async def test_failed_checks_are_not_task_success(tmp_path: Path) -> None:
    destination = await run(tmp_path, Adapter("fail"))
    assert all(record["status"] == "failed" for record in read_records(destination))
    report = (destination / "evaluation_report.md").read_text(encoding="utf-8")
    assert "NOT real model" in report
    assert "Checks passing is not task success" in report


async def test_cancellation_persists_partial_run_and_propagates(tmp_path: Path) -> None:
    with pytest.raises(asyncio.CancelledError):
        await run(tmp_path, Adapter("cancel"))
    directory = next((tmp_path / "runs").iterdir())
    assert [record["status"] for record in read_records(directory)] == ["cancelled"]
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "cancelled"
    assert (directory / "evaluation_report.md").is_file()


async def test_new_runs_never_overwrite_existing_results(tmp_path: Path) -> None:
    first = await run(tmp_path, Adapter())
    original = (first / "cases.jsonl").read_bytes()
    second = await run(tmp_path, Adapter())
    assert first != second
    assert (first / "cases.jsonl").read_bytes() == original


async def test_invalid_cases_and_duplicate_variants_fail_before_creating_run(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="unique"):
        await run_evaluation(
            suite(),
            dataset_sha256="a" * 64,
            adapters=[Adapter(), Adapter()],
            configuration=RunConfiguration(),
            output_root=tmp_path / "runs",
            repo_root=tmp_path,
        )
    assert not (tmp_path / "runs").exists()
