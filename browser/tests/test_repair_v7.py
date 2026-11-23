"""Fresh one-control allowance; retain consumed v3-v6 results and strict grading."""

import json
from pathlib import Path

import httpx
import pytest

from autoscholar.evaluation import repair_validation as repair
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.repair_protocol import RepairLedger, repair_claim, repair_contract
from tests.test_repair_v4 import proposal, write


def test_v7_preserves_prior_contracts_and_only_schedules_the_missing_control() -> None:
    before = {
        3: "cf21e242b88c5284060dbc1124e8e0f7b9bd0f9e5a210705199242523b1dc9ea",
        4: "b9e8505151ce8b31b48fa53ef3a57ffe9e6bcd6c06733a1ed099f8a2bac41354",
        5: "bcb7d7a273ce1135fdcc02c1fbec0d3585bdbbaf573a3ef93326a376bb29de73",
        6: "62181d576cf809365ea34628ed26482e213e7b6662553b3759ebfef5cbbd1321",
    }
    for version, digest in before.items():
        assert payload_digest(repair_contract(version)) == digest
    old, current = repair_contract(6), repair_contract(7)
    assert current["tasks"] == ["recover-no_replanning"]
    assert current["physical_limits"] == {"llm": 12, "tavily": 0, "per_task_llm": 12}
    for key in ("limits", "request_admission", "max_output_tokens", "runtime_schema_sha256"):
        assert current[key] == old[key]
    assert (
        current["acceptance"]["recover-no_replanning"] == old["acceptance"]["recover-no_replanning"]
    )
    assert current["coding_convergence"]["max_snapshot_edit_calls"] == 8
    assert current["phase10_overall_acceptance"] is False


@pytest.mark.parametrize("version", [3, 4, 5, 6])
def test_v7_requires_every_consumed_claim_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    path = proposal(tmp_path, monkeypatch, 7)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["protected_previous_records_sha256"][repair_claim(version)]
    write(path, value)
    with pytest.raises(ValueError, match="consumed prior"):
        repair.prepare_repairs(tmp_path, 7, path)
    assert not list((tmp_path / "data/evaluation").glob("repair-plan-*"))


@pytest.mark.parametrize("changed", ["v6_claim", "v6_grade", "v6_grant"])
def test_v7_prior_record_changes_block_execution_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    import hashlib

    path = proposal(tmp_path, monkeypatch, 7)
    base = tmp_path / "data/evaluation"
    value = json.loads(path.read_text(encoding="utf-8"))
    name = {
        "v6_claim": repair_claim(6),
        "v6_grade": "prior-v6/acceptance.json",
        "v6_grant": "provider-approvals/old-v6.json",
    }[changed]
    target = base / name
    if changed != "v6_claim":
        write(target, {"targeted_repairs_acceptance": False, "consumed": 29})
    value["protected_previous_records_sha256"][name] = hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    write(path, value)
    prepared = repair.prepare_repairs(tmp_path, 7, path)
    write(target, {"changed": True})
    with pytest.raises(ValueError, match="Prior evidence"):
        repair.load_repair_plan(prepared / "plan.json", tmp_path, 7)
    assert not (base / repair_claim(7)).exists()


def test_v7_grant_cannot_dispatch_other_cases_or_expand_the_allowance(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "fake-v7.json", run_id="mock-control", protocol_version=7)
    assert ledger.limits == {"llm": 12, "tavily": 0}
    assert ledger.data["tasks"] == ["recover-no_replanning"]
    for task in ("normal-baseline", "recover-baseline", "deception-baseline"):
        with pytest.raises(ValueError):
            ledger.reserve("llm", task + "--1", {})
    assert not ledger.attempts
    for ordinal in range(1, 13):
        ledger.reserve("llm", f"recover-no_replanning--{ordinal}", {})
    with pytest.raises(ValueError):
        ledger.reserve("llm", "recover-no_replanning--13", {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", "recover-no_replanning--1", {})


@pytest.mark.parametrize("change", ["scope", "version", "ceiling"])
def test_v7_output_cap_requires_the_exact_single_control_grant(tmp_path: Path, change: str) -> None:
    ledger = RepairLedger(tmp_path / "fake-v7.json", run_id="mock-control", protocol_version=7)
    GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    if change == "scope":
        ledger.data["scope"] = "unapproved"
    elif change == "version":
        ledger.data["protocol_version"] = 6
    else:
        ledger.limits["llm"] = 36
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


@pytest.mark.parametrize(
    "failure", ["coding", "no_fault", "wrong_stop", "rerun", "extra_code", "valid"]
)
def test_v7_control_requires_actual_fault_and_expected_stop(tmp_path: Path, failure: str) -> None:
    ledger = RepairLedger(tmp_path / "fake-v7.json", run_id="mock-control", protocol_version=7)
    ledger.reserve("llm", "recover-no_replanning--1", {})
    row = {
        "task": "recover-no_replanning",
        "status": "measured",
        "task_success": False,
        "physical_accounting_verified": True,
        "intervention_verified": True,
        "fault_mutations": 1,
        "error_code": "evaluation_replanning_disabled",
        "step_counts": {"research": 1, "code": 1, "train": 1},
        "usage": {"training_runs": 1},
    }
    if failure == "coding":
        row.update(
            fault_mutations=0, error_code="coding_tool_budget_exceeded", usage={"training_runs": 0}
        )
    elif failure == "no_fault":
        row["fault_mutations"] = 0
    elif failure == "wrong_stop":
        row["error_code"] = "autonomous_budget_exceeded"
    elif failure == "rerun":
        row["usage"] = {"training_runs": 2}
    elif failure == "extra_code":
        row["step_counts"] = {"research": 1, "code": 2, "train": 1}
    summary = repair.repair_summary([row], ledger, clean=True, unchanged=True, protocol_version=7)
    assert summary["planned_tasks"] == 1
    assert summary["targeted_repairs_acceptance"] is (failure == "valid")
    assert summary["phase10_overall_acceptance"] is False
