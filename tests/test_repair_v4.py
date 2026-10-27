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


def proposal(root: Path, monkeypatch: pytest.MonkeyPatch, version: int = 4) -> Path:
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
    contract = repair_contract(version)
    contract["scope"] += "_proposal"
    value: dict[str, Any] = {
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
    if version >= 5:
        claim = base / "live-repair-v4-claim.json"
        write(claim, {"scope": "old-v4", "attempts": 14})
        artifact = base / "prior/workflow.sqlite"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"binary historical database\x00\xff")
        for path in (claim, artifact):
            value["protected_previous_records_sha256"][path.relative_to(base).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
            )
    for old_version in range(5, version):
        claim = base / f"live-repair-v{old_version}-claim.json"
        write(claim, {"scope": f"old-v{old_version}", "attempts": 29})
        value["protected_previous_records_sha256"][claim.name] = hashlib.sha256(
            claim.read_bytes()
        ).hexdigest()
    target = base / "proposal/plan.json"
    write(target, value)
    return target


@pytest.mark.parametrize("version", [4, 5, 6])
def test_offline_plan_binds_proposal_and_retains_old_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    path = proposal(tmp_path, monkeypatch, version)
    previous = tmp_path / "data/evaluation/live-repair-v3-claim.json"
    old = previous.read_bytes()
    directory = repair.prepare_repairs(tmp_path, version, path)
    plan = repair.load_repair_plan(directory / "plan.json", tmp_path, version)
    assert plan["contract"] == repair_contract(version)
    assert plan["approved_proposal"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert previous.read_bytes() == old
    assert not (tmp_path / "data/evaluation" / repair_claim(version)).exists()
    assert not (tmp_path / "data/evaluation/provider-approvals").exists()
    with pytest.raises(ValueError, match="changed"):
        repair.load_repair_plan(directory / "plan.json", tmp_path, 3)


@pytest.mark.parametrize(
    "changed", ["proposal", "old_claim", "runtime", "fixture", "dirty", "head"]
)
@pytest.mark.parametrize("version", [4, 5, 6])
def test_plan_blocks_every_changed_binding_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
    version: int,
) -> None:
    path = proposal(tmp_path, monkeypatch, version)
    directory = repair.prepare_repairs(tmp_path, version, path)
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
        repair.load_repair_plan(directory / "plan.json", tmp_path, version)


@pytest.mark.parametrize("version", [4, 5, 6])
def test_requires_proposal_before_any_output(tmp_path: Path, version: int) -> None:
    with pytest.raises(ValueError, match="approved proposal"):
        repair.prepare_repairs(tmp_path, version)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("version", [0, 1, 2, 8, True, 4.0])
def test_arbitrary_versions_cannot_reset_a_claim(version: int) -> None:
    with pytest.raises(ValueError):
        repair_claim(version)


@pytest.mark.parametrize("changed", ["missing_v4_claim", "v4_claim", "binary_artifact", "escape"])
def test_v5_protects_consumed_claims_and_binary_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    path = proposal(tmp_path, monkeypatch, 5)
    value = json.loads(path.read_text(encoding="utf-8"))
    if changed == "missing_v4_claim":
        del value["protected_previous_records_sha256"]["live-repair-v4-claim.json"]
    elif changed == "escape":
        value["protected_previous_records_sha256"]["../../outside"] = "0" * 64
    if changed in {"missing_v4_claim", "escape"}:
        write(path, value)
        with pytest.raises(ValueError):
            repair.prepare_repairs(tmp_path, 5, path)
        return
    directory = repair.prepare_repairs(tmp_path, 5, path)
    target = (
        tmp_path
        / "data/evaluation"
        / ("live-repair-v4-claim.json" if changed == "v4_claim" else "prior/workflow.sqlite")
    )
    target.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Prior evidence"):
        repair.load_repair_plan(directory / "plan.json", tmp_path, 5)


def test_v5_does_not_change_old_contracts_or_widen_limits() -> None:
    old, current = repair_contract(4), repair_contract(5)
    assert old["scope"] == "phase10_reviewer_retry_acceptance_v4"
    assert "request_admission" not in old
    assert current["physical_limits"] == old["physical_limits"]
    assert current["limits"] == old["limits"] and current["acceptance"] == old["acceptance"]
    assert current["runtime_schema_sha256"] == old["runtime_schema_sha256"]
    assert current["request_admission"]["source_truncation"] is False


@pytest.mark.parametrize("version", [4, 5, 6])
def test_keeps_the_same_physical_ceiling_and_output_cap(tmp_path: Path, version: int) -> None:
    ledger = RepairLedger(tmp_path / "grant.json", run_id="mock-only", protocol_version=version)
    assert ledger.limits == {"llm": 36, "tavily": 0}
    assert len({repair_claim(v) for v in (3, 4, 5)}) == 3
    GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    for task in repair_contract(version)["tasks"]:
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
