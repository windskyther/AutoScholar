"""Acceptance-pack contracts only; stubs NEVER claim model or training verification."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import JsonValue

from autoscholar.core.config import Settings
from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation import pack as pack_module
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    MeasuredUsage,
    Observation,
    RunConfiguration,
    ScoreCard,
)
from autoscholar.evaluation.pack import JOBS, inspect_run, run_pack
from autoscholar.evaluation.runner import run_evaluation

SOURCE_ROOT = cli.REPO_ROOT
REVISION = "a" * 40
VARIANTS = {
    "rag_replay": ("dense", "sparse", "hybrid", "hybrid_rerank"),
    "workflow_ablations": (
        "baseline",
        "no_planner",
        "no_reviewer",
        "no_memory",
        "no_replanning",
    ),
    "retrieval_ablations": ("baseline", "no_reranker"),
}


class ContractAdapter:
    def __init__(self, identity: AdapterIdentity, action: str = "pass") -> None:
        self.identity = identity
        self.action = action

    def validate_case(self, case: BenchmarkCase) -> None:
        pass

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        if self.action == "cancel":
            raise asyncio.CancelledError()
        usage = MeasuredUsage(
            model_calls=0,
            external_api_calls=0,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
        )
        if self.action == "unknown":
            usage.external_api_calls = None
        elif self.action == "external":
            usage.external_api_calls = 1
        return Observation(payload={}, usage=usage)

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return ScoreCard(
            checks={"contract_stub": self.action != "fail"},
            metrics={"task_completion": 0.0} if self.identity.category == "end_to_end" else {},
            task_success=False if self.identity.category == "end_to_end" else None,
        )


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prepared: dict[str, cli.PreparedEvaluation] = {}
        self.actions: dict[str, str] = {}
        self.executed: list[str] = []
        self.preflight_error: str | None = None
        self.resource_drift = False

    async def prepare(self, args: argparse.Namespace) -> cli.PreparedEvaluation:
        job = next(item for item in JOBS if item.id == args.output_root.name)
        if job.id == self.preflight_error:
            raise ValueError("private-key-in-preflight-must-not-be-exported")
        dataset = SOURCE_ROOT / (
            f"benchmarks/ablations/{'workflow' if job.category == 'end_to_end' else 'retrieval'}"
            "_v1.json"
            if job.ablations
            else f"benchmarks/{job.category}/public_v1.json"
        )
        suite, digest = load_suite(dataset)
        variants = VARIANTS.get(job.id, ("baseline",))
        resources = {"input_fixture": "0" * 64}
        if job.category in ("coding", "experiment", "end_to_end"):
            resources["sandbox_image"] = (
                "2" * 64 if job.id == "experiment" and self.resource_drift else "1" * 64
            )
            if job.category != "coding":
                resources["dataset"] = "3" * 64
        prepared = cli.PreparedEvaluation(
            suite,
            digest,
            [
                ContractAdapter(
                    AdapterIdentity(
                        name="pack-contract-stub",
                        category=job.category,
                        variant=variant,
                        execution="fixture" if job.profile == "offline" else "injected",
                        resources=resources,
                    ),
                    self.actions.get(job.id, "pass"),
                )
                for variant in variants
            ],
            RunConfiguration(
                profile=job.profile,
                seed=args.seed,
                repeats=args.repeats,
                case_timeout_seconds=args.timeout,
            ),
            args.output_root,
        )
        self.prepared[job.id] = prepared
        return prepared

    async def evaluate(self, prepared: cli.PreparedEvaluation, *, ablations: bool) -> Path:
        job_id = prepared.destination.name
        self.executed.append(job_id)
        if self.actions.get(job_id) == "error":
            raise RuntimeError("private-key-and-private-prompt-must-never-be-exported")
        directory = await run_evaluation(
            prepared.suite,
            dataset_sha256=prepared.digest,
            adapters=prepared.adapters,
            configuration=prepared.configuration,
            output_root=prepared.destination,
            repo_root=self.root,
        )
        if self.actions.get(job_id) == "tamper":
            (directory / "cases.jsonl").write_text("{}\n", encoding="utf-8")
        return directory


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    result = Harness(tmp_path)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_prepare", result.prepare)
    monkeypatch.setattr(cli, "_evaluate", result.evaluate)
    monkeypatch.setattr(pack_module, "_git_state", lambda root: (REVISION, False))
    monkeypatch.setattr("autoscholar.evaluation.runner._git_state", lambda root: (REVISION, False))

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Pack must not instantiate settings or HTTP providers")

    monkeypatch.setattr(Settings, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setenv("LLM_API_KEY", "never-export-this-key")
    monkeypatch.setenv("TAVILY_API_KEY", "never-export-this-search-key")
    return result


def arguments(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            "sandbox_container": "autoscholar-eval-test-sandbox-manager-1",
            "seed": 42,
            "repeats": 1,
            "timeout": 360.0,
            "output_root": None,
            **kwargs,
        }
    )


def summary(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "summary.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


async def test_pack_runs_all_eight_slots_without_mixing_task_success(harness: Harness) -> None:
    directory, accepted = await run_pack(arguments(), repo_root=harness.root)
    result = summary(directory)
    assert accepted and result["engineering_acceptance"]
    assert result["phase10_overall_acceptance"] is False
    assert result["planned"] == result["recorded"] == result["passed"] == 155
    assert harness.executed == [job.id for job in JOBS]
    assert "task_success_rate" not in result
    assert result["usage"]["external_api_calls"]["total"] == 0
    assert result["usage"]["monetary_cost"]["total"] is None
    report = (directory / "evaluation_report.md").read_text(encoding="utf-8")
    assert "Overall Phase 10 acceptance: **NOT VERIFIED**" in report
    assert "unknown" in report
    assert "no_reranker" in report and "no_replanning" in report
    assert "never-export" not in report
    for slot in result["slots"]:
        assert slot["primary_sha256"]
        assert (directory / slot["run"] / "evaluation_report.md").is_file()
        for group in slot["summary"]["groups"]:
            assert group["task_success_rate"] == (0.0 if slot["category"] == "end_to_end" else None)


async def test_repeat_seed_and_run_outputs_are_distinct(harness: Harness) -> None:
    first, accepted = await run_pack(arguments(repeats=2, seed=2**32 - 1), repo_root=harness.root)
    assert accepted and summary(first)["planned"] == 310
    run = first / summary(first)["slots"][0]["run"]
    lines = (run / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["seed"] for line in lines} == {2**32 - 1, 0}
    second, accepted = await run_pack(arguments(), repo_root=harness.root)
    assert accepted and second != first and first.is_dir()


@pytest.mark.parametrize("action", ["fail", "unknown", "external"])
async def test_failure_or_unknown_usage_cannot_pass_pack(harness: Harness, action: str) -> None:
    harness.actions["tool"] = action
    directory, accepted = await run_pack(arguments(), repo_root=harness.root)
    result = summary(directory)
    assert not accepted
    assert result["state"] == "completed" and result["recorded"] == 155
    if action == "unknown":
        assert result["usage"]["external_api_calls"]["total"] is None
        assert result["usage"]["external_api_calls"]["measured_records"] == 135


async def test_preflight_failure_leaves_no_started_run(harness: Harness) -> None:
    harness.preflight_error = "coding"
    with pytest.raises(ValueError):
        await run_pack(arguments(), repo_root=harness.root)
    assert not harness.executed
    assert not (harness.root / "data/evaluation").exists()


async def test_pinned_resources_must_match_before_any_execution(harness: Harness) -> None:
    harness.resource_drift = True
    with pytest.raises(ValueError, match="resources changed"):
        await run_pack(arguments(), repo_root=harness.root)
    assert not harness.executed


@pytest.mark.parametrize("action", ["error", "tamper"])
async def test_infrastructure_failure_stops_and_redacts(harness: Harness, action: str) -> None:
    harness.actions["coding"] = action
    directory, accepted = await run_pack(arguments(), repo_root=harness.root)
    result = summary(directory)
    assert not accepted and result["state"] == "error"
    assert result["planned"] == 155 and result["recorded"] == 110
    assert len(harness.executed) == 4
    assert result["usage"]["external_api_calls"]["total"] is None
    assert all(item["state"] == "pending" for item in result["slots"][4:])
    for name in ("pack_manifest.json", "summary.json", "evaluation_report.md"):
        assert "private-key" not in (directory / name).read_text(encoding="utf-8")


async def test_cancellation_keeps_partial_primary_record_and_unrun_denominator(
    harness: Harness,
) -> None:
    harness.actions["coding"] = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await run_pack(arguments(), repo_root=harness.root)
    directory = next((harness.root / "data/evaluation").iterdir())
    result = summary(directory)
    assert not result["engineering_acceptance"] and result["state"] == "cancelled"
    assert result["planned"] == 155 and result["recorded"] == 111
    slot = result["slots"][3]
    assert slot["state"] == "cancelled" and slot["recorded"] == 1
    assert json.loads((directory / slot["run"] / "manifest.json").read_text())["state"] == (
        "cancelled"
    )
    assert all(item["state"] == "pending" for item in result["slots"][4:])


@pytest.mark.parametrize("git", [(None, None), (REVISION, True)])
async def test_unknown_or_dirty_source_is_not_acceptance(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, git: tuple[str | None, bool | None]
) -> None:
    monkeypatch.setattr(pack_module, "_git_state", lambda root: git)
    with pytest.raises(ValueError, match="clean committed"):
        await run_pack(arguments(), repo_root=harness.root)
    assert not harness.prepared


async def test_source_changed_at_end_prevents_acceptance(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def git(root: Path) -> tuple[str, bool]:
        nonlocal calls
        calls += 1
        return (REVISION, calls >= 3)

    monkeypatch.setattr(pack_module, "_git_state", git)
    directory, accepted = await run_pack(arguments(), repo_root=harness.root)
    assert not accepted and summary(directory)["source_unchanged"] is False


async def test_output_escape_is_rejected(harness: Harness) -> None:
    with pytest.raises(ValueError, match="escaped"):
        await run_pack(arguments(output_root=harness.root / "outside"), repo_root=harness.root)
    assert not harness.prepared


async def test_primary_record_validation_does_not_trust_sidecars(harness: Harness) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][2]["run"]
    (run / "summary.json").write_text('{"engineering_acceptance": true}', encoding="utf-8")
    (run / "evaluation_report.md").write_text("untrusted-private-content", encoding="utf-8")
    evidence = inspect_run(harness.prepared["tool"], run, revision=REVISION)
    assert len(evidence.results) == 20


@pytest.mark.parametrize(
    ("target", "key", "value"),
    [
        ("manifest", "git_revision", "b" * 40),
        ("manifest", "git_dirty", True),
        ("manifest", "dataset_sha256", "b" * 64),
        ("manifest", "suite_id", "different-suite"),
        ("manifest", "state", "running"),
        ("manifest", "completed_at", None),
        ("case", "case_id", "unscheduled"),
        ("case", "seed", 17),
        ("case", "repeat", 2),
        ("case", "input_sha256", "b" * 64),
        ("case", "expected_sha256", "b" * 64),
        ("case", "status", "failed"),
    ],
)
async def test_wrong_revision_case_seed_gold_or_outcome_is_invalid(
    harness: Harness, target: str, key: str, value: object
) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][2]["run"]
    path = run / ("manifest.json" if target == "manifest" else "cases.jsonl")
    if target == "manifest":
        payload = json.loads(path.read_text())
        payload[key] = value
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[0][key] = value
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_run(harness.prepared["tool"], run, revision=REVISION)


async def test_duplicate_or_nonfinite_records_are_invalid(harness: Harness) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][2]["run"]
    path = run / "cases.jsonl"
    records = path.read_text().splitlines()
    path.write_text("\n".join([records[0], *records[:-1]]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        inspect_run(harness.prepared["tool"], run, revision=REVISION)
    records[0] = records[0].replace('"duration_ms":', '"duration_ms":NaN,"unused":')
    path.write_text("\n".join(records), encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_run(harness.prepared["tool"], run, revision=REVISION)


@pytest.mark.parametrize("mutation", ["resources", "category", "task", "duplicate_key"])
async def test_identity_task_scope_and_duplicate_keys_are_validated(
    harness: Harness, mutation: str
) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][2]["run"]
    path = run / "cases.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "resources":
        records[0]["adapter"]["resources"]["input_fixture"] = "f" * 64
    elif mutation == "category":
        records[0]["category"] = "research"
    elif mutation == "task":
        records[0]["score"]["task_success"] = True
    text = "\n".join(json.dumps(record) for record in records)
    if mutation == "duplicate_key":
        text = text.replace('"case_id":', '"case_id": "duplicate", "case_id":', 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_run(harness.prepared["tool"], run, revision=REVISION)


async def test_missing_e2e_completion_is_invalid(harness: Harness) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][5]["run"]
    path = run / "cases.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[0]["score"]["task_success"] = None
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    with pytest.raises(ValueError, match="completion"):
        inspect_run(harness.prepared["end_to_end"], run, revision=REVISION)


async def test_evidence_is_bounded_and_cannot_escape_assigned_slot(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][2]["run"]
    monkeypatch.setattr(pack_module, "MAX_DATASET_BYTES", 32)
    with pytest.raises(ValueError, match="8 MiB"):
        inspect_run(harness.prepared["tool"], run, revision=REVISION)
    with pytest.raises(ValueError, match="escaped"):
        inspect_run(harness.prepared["coding"], run, revision=REVISION)


async def test_late_evidence_mutation_cannot_retain_acceptance(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def evaluate(prepared: cli.PreparedEvaluation, *, ablations: bool) -> Path:
        run = await harness.evaluate(prepared, ablations=ablations)
        if prepared.destination.name == "retrieval_ablations":
            earlier = next(harness.prepared["tool"].destination.iterdir())
            path = earlier / "cases.jsonl"
            path.write_text(path.read_text() + "\n", encoding="utf-8")
        return run

    monkeypatch.setattr(cli, "_evaluate", evaluate)
    directory, accepted = await run_pack(arguments(), repo_root=harness.root)
    result = summary(directory)
    assert not accepted and result["slots"][2]["state"] == "invalid"
    assert result["recorded"] == 135 and result["planned"] == 155


async def test_missing_cases_never_pass_even_if_manifest_completed(harness: Harness) -> None:
    directory, _ = await run_pack(arguments(), repo_root=harness.root)
    run = directory / summary(directory)["slots"][2]["run"]
    (run / "cases.jsonl").write_text("", encoding="utf-8")
    evidence = inspect_run(harness.prepared["tool"], run, revision=REVISION)
    assert not evidence.results
    manifest = pack_module.PackManifest.model_validate_json(
        (directory / "pack_manifest.json").read_text(encoding="utf-8")
    )
    result = pack_module.pack_summary(manifest, {"tool": evidence})
    assert result["engineering_acceptance"] is False
    assert result["planned"] == 155 and result["recorded"] == 0


def test_pack_cli_exit_and_configuration_errors(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["eval", "pack", "--sandbox-container", "test"])
    assert cli.main() == 0
    assert "NOT overall Phase 10" in capsys.readouterr().out
    harness.preflight_error = "coding"
    assert cli.main() == 2
    assert "private-key" not in capsys.readouterr().out
