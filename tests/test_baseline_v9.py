import argparse
import json
from pathlib import Path

import pytest

from autoscholar.evaluation import paired_validation as paired
from autoscholar.evaluation.baseline_protocol import CLAIM, BaselineLedger, baseline_contract
from autoscholar.evaluation.provider_transport import GuardedTransport
from tests.test_evaluation_workflow import WorkflowReplaySandbox
from tests.test_llm_provider import configured_settings
from tests.test_paired_v8 import history, prepare


def test_one_shot_allowance_and_output_cap(tmp_path: Path) -> None:
    ledger = BaselineLedger(tmp_path / "grant.json", run_id="offline")
    for task in ("recover-baseline", "normal-no_planner"):
        with pytest.raises(ValueError):
            ledger.reserve("llm", task + "--1", {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", "normal-baseline--1", {})
    for ordinal in range(1, 13):
        ledger.reserve("llm", f"normal-baseline--{ordinal}", {})
    with pytest.raises(ValueError):
        ledger.reserve("llm", "normal-baseline--13", {})
    with pytest.raises(FileExistsError):
        BaselineLedger(tmp_path / "grant.json", run_id="reset")
    kwargs = dict(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
    )
    # Keep explicit typed calls below: constructor restrictions are independent of dispatch.
    GuardedTransport(
        provider="llm",
        endpoint=str(kwargs["endpoint"]),
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
    )
    ledger.limits["llm"] = 13
    with pytest.raises(ValueError):
        GuardedTransport(
            provider="llm",
            endpoint=str(kwargs["endpoint"]),
            ledger=ledger,
            output=tmp_path,
            secrets=(),
            max_output_tokens=8192,
        )


@pytest.mark.parametrize("changed", ["source", "history", "contract", "version"])
def test_frozen_plan_rejects_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    path = prepare(tmp_path, monkeypatch, 9)
    if changed == "source":
        monkeypatch.setattr(paired, "_git_state", lambda root: ("c" * 40, False))
    elif changed == "history":
        (tmp_path / "data/evaluation/live-paired-v8-claim.json").write_text("{}")
    elif changed == "contract":
        value = json.loads(path.read_text())
        value["contract"]["physical_limits"]["llm"] = 13
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        paired.load_paired_plan(path, tmp_path, 8 if changed == "version" else 9)
    assert not (tmp_path / "data/evaluation" / CLAIM).exists()


def test_preparation_requires_old_v8_claim_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Free preparation must not load secrets")

    monkeypatch.setattr(paired, "load_explicit_settings", forbidden)
    path = prepare(tmp_path, monkeypatch, 9)
    assert paired.load_paired_plan(path, tmp_path, 9)["contract"] == baseline_contract()
    with pytest.raises(ValueError, match="consumed v8"):
        paired.prepare_paired(tmp_path, history(tmp_path), 9)
    assert not (tmp_path / "data/evaluation/provider-approvals").exists()


async def test_resource_mismatch_stops_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepare(tmp_path, monkeypatch, 9)
    monkeypatch.setattr(
        paired, "load_explicit_settings", lambda *a, **k: (configured_settings(), None)
    )
    monkeypatch.setattr(paired, "DockerEvaluationSandbox", lambda *a: WorkflowReplaySandbox())
    monkeypatch.setattr(paired, "sandbox_resources", lambda *a, **k: {"changed": "resource"})
    args = argparse.Namespace(
        allow_paid=True,
        plan=path,
        protocol_version=9,
        approval_id="offline-resource-check",
        sandbox_container="fake",
    )
    with pytest.raises(ValueError, match="resources changed"):
        await paired.run_paired(args, repo_root=tmp_path)
    assert not (tmp_path / "data/evaluation" / CLAIM).exists()
    assert not (tmp_path / "data/evaluation/provider-approvals").exists()
