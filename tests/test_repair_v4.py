"""No vendor calls: independently versioned approval, prior evidence and source bindings."""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.evaluation import repair_validation as repair
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.repair_protocol import RepairLedger, repair_claim, repair_contract


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def proposal(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Offline preparation must not initialize settings or Docker")

    monkeypatch.setattr(repair, "load_explicit_settings", forbidden)
    monkeypatch.setattr(repair, "DockerEvaluationSandbox", forbidden)
    monkeypatch.setattr(repair, "runner_only_changes", lambda *args: True)
    monkeypatch.setattr(repair, "_git_state", lambda *args: ("b" * 40, False))
    monkeypatch.setattr(repair, "load_fixture", lambda *args: (None, "c" * 64))
    base = root / "data/evaluation"
    previous = base / "live-repair-v3-claim.json"
    write(previous, {"scope": "old-v3", "attempts": 24})
    contract = repair_contract(4)
    contract["scope"] += "_proposal"
    value = {
        "schema_version": 1,
        "contract": contract,
        "git_revision": "a" * 40,
        "paid_execution_approved": False,
        "paid_execution_available": False,
        "external_api_calls": 0,
        "source_ready": True,
        "fixture_sha256": "c" * 64,
        "protected_previous_records_sha256": {
            previous.name: hashlib.sha256(previous.read_bytes()).hexdigest(),
        },
    }
    target = base / "proposal/plan.json"
    write(target, value)
    return target


def test_v4_offline_plan_binds_proposal_and_retains_old_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = proposal(tmp_path, monkeypatch)
    previous = tmp_path / "data/evaluation/live-repair-v3-claim.json"
    old = previous.read_bytes()
    directory = repair.prepare_repairs(tmp_path, 4, path)
    plan = repair.load_repair_plan(directory / "plan.json", tmp_path, 4)
    assert plan["contract"] == repair_contract(4)
    assert plan["approved_proposal"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert previous.read_bytes() == old
    assert not (tmp_path / "data/evaluation/live-repair-v4-claim.json").exists()
    assert not (tmp_path / "data/evaluation/provider-approvals").exists()
    with pytest.raises(ValueError, match="changed"):
        repair.load_repair_plan(directory / "plan.json", tmp_path, 3)


@pytest.mark.parametrize(
    "changed", ["proposal", "old_claim", "runtime", "fixture", "dirty", "head"]
)
def test_v4_plan_blocks_every_changed_binding_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    path = proposal(tmp_path, monkeypatch)
    directory = repair.prepare_repairs(tmp_path, 4, path)
    if changed == "proposal":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["unapproved_field"] = True
        write(path, value)
    elif changed == "old_claim":
        write(tmp_path / "data/evaluation/live-repair-v3-claim.json", {"reset": True})
    elif changed == "runtime":
        monkeypatch.setattr(repair, "runner_only_changes", lambda *args: False)
    elif changed == "fixture":
        monkeypatch.setattr(repair, "load_fixture", lambda *args: (None, "d" * 64))
    else:
        monkeypatch.setattr(
            repair,
            "_git_state",
            lambda *args: ("b" * 40, True) if changed == "dirty" else ("d" * 40, False),
        )
    with pytest.raises(ValueError):
        repair.load_repair_plan(directory / "plan.json", tmp_path, 4)


def test_v4_requires_proposal_before_any_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved proposal"):
        repair.prepare_repairs(tmp_path, 4)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("version", [0, 1, 2, 5, True, 4.0])
def test_arbitrary_versions_cannot_reset_a_claim(version: int) -> None:
    with pytest.raises(ValueError):
        repair_claim(version)


def test_v4_keeps_the_same_physical_ceiling_and_output_cap(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "grant.json", run_id="fake-v4", protocol_version=4)
    assert ledger.limits == {"llm": 36, "tavily": 0}
    assert repair_claim(3) != repair_claim(4)
    GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    for task in repair_contract(4)["tasks"]:
        for ordinal in range(1, 13):
            ledger.reserve("llm", f"{task}--{ordinal}", {})
        with pytest.raises(ValueError):
            ledger.reserve("llm", f"{task}--13", {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", "normal-baseline--1", {})
    ledger.data["scope"] = "unapproved"
    with pytest.raises(ValueError, match="Expanded output"):
        GuardedTransport(
            provider="llm",
            endpoint="https://example.org/chat/completions",
            ledger=ledger,
            output=tmp_path,
            secrets=(),
            max_output_tokens=8192,
            delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
        )
