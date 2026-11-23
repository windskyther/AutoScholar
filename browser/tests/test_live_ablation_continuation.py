"""Free continuation controls; fixture receipts are synthetic, never paid evidence."""

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoscholar.evaluation import live_ablation as live
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.live_ablation_continuation import (
    CLAIM_NAME,
    CONTINUATION_TASKS,
    V2_APPROVAL,
    ContinuationLedger,
    StoppedV2,
)
from autoscholar.evaluation.live_ablation_contract import LIMITS, TASKS
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.models import MeasuredUsage
from tests.test_live_ablation_v2 import fake_prior, fixture


def stopped_fixture(root: Path) -> StoppedV2:
    prior = fake_prior(root)
    folder = root / ("live-ablation-v2-" + "b" * 32)
    hashes = dict(prior.hashes)

    def write(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "configured_model": "fake-model",
        "protocol_version": 2,
        "objective_suffix": SUFFIX,
        "limits": LIMITS.model_dump(),
        "max_output_tokens": 8192,
        "tasks": list(TASKS),
        "prior_primary_sha256": prior.hashes,
    }
    write(folder / "manifest.json", manifest)
    write(
        root / "live-ablation-v2-claim.json",
        {
            "run_id": folder.name,
            "approval_file": V2_APPROVAL + ".json",
            "primary_sha256": prior.hashes,
        },
    )
    attempts = []
    records = []
    for index, count in enumerate((8, 6, 8, 9, 8, 10, 7, 8)):
        task = TASKS[index]
        for ordinal in range(1, count + 1):
            identifier = f"{task}--{ordinal}"
            request = {"model": "fake-model", "max_tokens": 8192, "messages": []}
            attempts.append(
                {
                    "case_id": identifier,
                    "provider": "llm",
                    "state": "returned",
                    "http_status": 200,
                    "request_sha256": payload_digest(request),
                    "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                }
            )
            write(folder / f"request-llm-{identifier}.json", request)
            write(
                folder / f"response-llm-{identifier}.json",
                {
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                },
            )
        records.append(
            {
                "task": task,
                "physical_requests": count,
                "status": "measured" if index < 7 else "provider_error",
                "task_success": False if index < 7 else None,
                "physical_accounting_verified": index < 7,
                "usage": {
                    "model_calls": count if index < 7 else 9,
                    "input_tokens": 2 * count,
                    "output_tokens": count,
                    "total_tokens": 3 * count,
                },
                "input_sha256": task.split("-")[0],
                "observed_models": ["fake"],
            }
        )
        write(folder / task / "workflow.json", {})
    ledger = {"run_id": folder.name, "limits": {"llm": 159, "tavily": 0}, "attempts": attempts}
    write(root / "provider-approvals" / (V2_APPROVAL + ".json"), ledger)
    write(folder / "request_ledger.json", ledger)
    write(folder / "cases.json", records)
    write(
        folder / "acceptance.json", {"live_pilot_measurement_complete": False, "vendor_tokens": 192}
    )
    # Exclude the additive audit itself from its primary hash map.
    write(folder / "audit-20261021.json", {"primary_sha256": dict(hashes)})
    return StoppedV2(folder, prior)


def test_exact_84_new_requests_and_no_reset(tmp_path: Path) -> None:
    stopped = stopped_fixture(tmp_path)
    ledger = ContinuationLedger(tmp_path / "new.json", run_id="fake", stopped=stopped, manifest={})
    for task in CONTINUATION_TASKS:
        for ordinal in range(1, 13):
            ledger.reserve("llm", f"{task}--{ordinal}", {})
    assert len(ledger.attempts) == 84
    assert len(ledger.attempts) + ledger.data["prior_requests"] == 169
    with pytest.raises(ValueError):
        ledger.reserve("llm", CONTINUATION_TASKS[-1] + "--13", {})
    with pytest.raises(FileExistsError):
        ContinuationLedger(tmp_path / "different.json", run_id="new", stopped=stopped, manifest={})
    assert stopped.unchanged()
    assert (tmp_path / CLAIM_NAME).exists()


@pytest.mark.parametrize("task", TASKS[:8])
def test_every_old_task_including_interruption_is_forbidden(tmp_path: Path, task: str) -> None:
    stopped = stopped_fixture(tmp_path)
    ledger = ContinuationLedger(tmp_path / "new.json", run_id="fake", stopped=stopped, manifest={})
    with pytest.raises(ValueError, match="previously attempted"):
        ledger.reserve("llm", task + "--1", {})
    assert not ledger.attempts


@pytest.mark.parametrize(
    "target",
    [
        "cases.json",
        "manifest.json",
        "response-llm-recover-no_reviewer--8.json",
        "audit-20261021.json",
    ],
)
def test_changed_prior_evidence_blocks_dispatch(tmp_path: Path, target: str) -> None:
    stopped = stopped_fixture(tmp_path)
    ledger = ContinuationLedger(tmp_path / "new.json", run_id="fake", stopped=stopped, manifest={})
    (stopped.directory / target).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        ledger.reserve("llm", CONTINUATION_TASKS[0] + "--1", {})
    assert not ledger.attempts


def test_tavily_and_out_of_order_dispatch_are_forbidden(tmp_path: Path) -> None:
    stopped = stopped_fixture(tmp_path)
    ledger = ContinuationLedger(tmp_path / "new.json", run_id="fake", stopped=stopped, manifest={})
    for provider, identifier in (("tavily", "--1"), ("llm", "--2")):
        with pytest.raises(ValueError):
            ledger.reserve(provider, CONTINUATION_TASKS[0] + identifier, {})  # type: ignore[arg-type]
    assert not ledger.attempts


async def test_continuation_requires_v2_before_any_sandbox_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "_git_state", lambda root: ("source", False))
    with pytest.raises(ValueError, match="requires protocol v2"):
        await live.run_live_ablations(
            argparse.Namespace(
                allow_paid=True,
                approval_id="test",
                protocol_version=1,
                continue_v2=tmp_path,
            ),
            repo_root=tmp_path,
        )


def test_original_unknown_is_not_promoted_by_combined_summary(tmp_path: Path) -> None:
    stopped = stopped_fixture(tmp_path)
    ledger = ContinuationLedger(tmp_path / "new.json", run_id="fake", stopped=stopped, manifest={})
    new: list[dict[str, Any]] = [
        dict(
            task=task,
            status="measured",
            task_success=True,
            input_sha256=task.split("-")[0],
            observed_models=["fake"],
            physical_accounting_verified=True,
            intervention_verified=True,
        )
        for task in CONTINUATION_TASKS
    ]
    result = live.pilot_summary(stopped.records + new, ledger, clean=True, unchanged=True)
    assert not result["live_pilot_measurement_complete"]
    assert not result["phase10_overall_acceptance"]
    assert result["contrasts"][1]["paired"] == 2
    assert stopped.records[-1]["task_success"] is None


async def test_cli_runner_dispatches_only_seven_and_preserves_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data/evaluation"
    stopped = stopped_fixture(root)
    stopped.manifest.update(resources={}, fixture_sha256="fixture")
    monkeypatch.setattr(live, "StoppedV2", lambda *args: stopped)
    monkeypatch.setattr(live, "_git_state", lambda root: ("fake-source", False))
    monkeypatch.setattr(live, "load_fixture", lambda *args: (fixture(), "fixture"))
    monkeypatch.setattr(live, "sandbox_resources", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        live,
        "load_explicit_settings",
        lambda *args, **kwargs: (
            SimpleNamespace(
                llm_model="fake-model",
                llm_base_url="https://example.org",
                llm_api_key=SimpleNamespace(get_secret_value=lambda: "fake-key"),
            ),
            None,
        ),
    )
    monkeypatch.setattr(live, "OpenAICompatibleProvider", lambda *args, **kwargs: None)

    class Sandbox:
        async def health(self) -> None:
            pass

        async def close(self) -> None:
            pass

    async def clean(sandbox: Any) -> bool:
        return True

    monkeypatch.setattr(live, "DockerEvaluationSandbox", lambda *args: Sandbox())
    monkeypatch.setattr(live, "no_owned_children", clean)
    dispatched = []

    async def fake_execute(**kwargs: Any) -> dict[str, Any]:
        provider = kwargs["provider"]
        dispatched.append(provider.task)
        provider.calls = 1
        ledger = provider.transport.ledger
        index = ledger.reserve("llm", provider.task + "--1", {})
        ledger.finish(
            index,
            state="returned",
            status=200,
            usage=MeasuredUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )
        return dict(
            status="measured",
            task_success=False,
            intervention_verified=True,
            observed_models=["fake"],
            usage=dict(model_calls=1, input_tokens=2, output_tokens=1, total_tokens=3),
        )

    monkeypatch.setattr(live, "execute_task", fake_execute)
    directory, complete = await live.run_live_ablations(
        argparse.Namespace(
            allow_paid=True,
            approval_id="fake-tail",
            protocol_version=2,
            prior_run=root / ("live-ablation-" + "a" * 32),
            continue_v2=stopped.directory,
            sandbox_container="fake-controller",
        ),
        repo_root=tmp_path,
    )
    assert complete and dispatched == list(CONTINUATION_TASKS)
    summary = json.loads((directory / "acceptance.json").read_text(encoding="utf-8"))
    assert summary["continuation_measurement_complete"]
    assert summary["cumulative_llm_requests"] == 92
    assert not summary["live_pilot_measurement_complete"]
    combined = json.loads((directory / "combined_cases.json").read_text(encoding="utf-8"))
    assert len(combined) == 15 and combined[7]["task_success"] is None
    assert all(row["source_run"] == stopped.directory.name for row in combined[:8])
    assert stopped.unchanged()
