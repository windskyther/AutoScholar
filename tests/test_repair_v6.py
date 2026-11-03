"""Fresh v6 scope; old consumed grants and live outcomes remain immutable. No APIs."""

import hashlib
import json
from pathlib import Path

import pytest

from autoscholar.evaluation import repair_protocol
from autoscholar.evaluation import repair_validation as repair
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.repair_protocol import repair_claim, repair_contract
from tests.test_repair_v4 import proposal, write


def test_v6_does_not_change_prior_contracts_or_acceptance_thresholds() -> None:
    before = {
        3: "cf21e242b88c5284060dbc1124e8e0f7b9bd0f9e5a210705199242523b1dc9ea",
        4: "b9e8505151ce8b31b48fa53ef3a57ffe9e6bcd6c06733a1ed099f8a2bac41354",
        5: "bcb7d7a273ce1135fdcc02c1fbec0d3585bdbbaf573a3ef93326a376bb29de73",
    }
    for version, digest in before.items():
        assert payload_digest(repair_contract(version)) == digest
    old, current = repair_contract(5), repair_contract(6)
    for key in (
        "physical_limits",
        "limits",
        "acceptance",
        "request_admission",
        "max_output_tokens",
        "runtime_schema_sha256",
    ):
        assert current[key] == old[key]
    assert current["scope"] == "phase10_recovery_authority_acceptance_v6"
    assert current["machine_recovery"]["max_unchanged_collection_retries"] == 1
    assert current["coding_review_evidence"]["unsupported_diagnoses_do_not_automatically_pass"]
    assert len({repair_claim(v) for v in (3, 4, 5, 6)}) == 4


@pytest.mark.parametrize("version", [3, 4, 5])
async def test_v6_requires_each_consumed_claim_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    path = proposal(tmp_path, monkeypatch, 6)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["protected_previous_records_sha256"][repair_claim(version)]
    write(path, value)
    with pytest.raises(ValueError, match="consumed prior"):
        repair.prepare_repairs(tmp_path, 6, path)
    assert not list((tmp_path / "data/evaluation").glob("repair-plan-*"))
    assert not (tmp_path / "data/evaluation" / repair_claim(6)).exists()


@pytest.mark.parametrize("changed", ["v5_claim", "v5_grant", "v5_result"])
def test_v6_rejects_any_changed_old_run_without_regrading_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    path = proposal(tmp_path, monkeypatch, 6)
    base = tmp_path / "data/evaluation"
    value = json.loads(path.read_text(encoding="utf-8"))
    targets = {
        "v5_claim": base / repair_claim(5),
        "v5_grant": base / "provider-approvals/old-v5.json",
        "v5_result": base / "old-v5/acceptance.json",
    }
    for name, target in targets.items():
        if name != "v5_claim":
            write(target, {"old": name, "targeted_repairs_acceptance": False})
        value["protected_previous_records_sha256"][target.relative_to(base).as_posix()] = (
            hashlib.sha256(target.read_bytes()).hexdigest()
        )
    write(path, value)
    prepared = repair.prepare_repairs(tmp_path, 6, path)
    write(targets[changed], {"changed": True})
    with pytest.raises(ValueError, match="Prior evidence"):
        repair.load_repair_plan(prepared / "plan.json", tmp_path, 6)
    assert not (base / repair_claim(6)).exists()
    assert not (base / "provider-approvals/new-v6.json").exists()


def test_v6_stage_duties_are_bound_into_the_executable_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = proposal(tmp_path, monkeypatch, 6)
    prepared = repair.prepare_repairs(tmp_path, 6, path)
    monkeypatch.setattr(repair_protocol, "stage_contracts", lambda: {"coding": "changed"})
    with pytest.raises(ValueError, match="changed"):
        repair.load_repair_plan(prepared / "plan.json", tmp_path, 6)
