"""Frozen repaired paired budget and historical/configuration admission; no vendor calls."""

import argparse
import json
from pathlib import Path

import pytest

from autoscholar.evaluation import paired_validation as paired
from autoscholar.evaluation.live_ablation_contract import TASKS
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.repaired_paired import (
    CLAIM,
    DAG_CLAIM,
    FINAL_CLAIM,
    RETRY_CLAIM,
    RepairedPairedLedger,
    repaired_paired_contract,
)
from tests.test_llm_provider import configured_settings
from tests.test_paired_v8 import history, prepare


@pytest.mark.parametrize("version", [11, 12, 13, 14])
def test_frozen_15_tasks_180_calls_and_no_grant_reset(tmp_path: Path, version: int) -> None:
    path = tmp_path / "mock-ledger.json"
    ledger = RepairedPairedLedger(path, run_id="mock-only", protocol_version=version)
    for task in TASKS:
        for ordinal in range(1, 13):
            ledger.reserve("llm", f"{task}--{ordinal}", {})
        with pytest.raises(ValueError):
            ledger.reserve("llm", task + "--13", {})
    assert len(ledger.attempts) == 180
    with pytest.raises(ValueError):
        ledger.reserve("tavily", TASKS[0] + "--1", {})
    with pytest.raises(FileExistsError):
        RepairedPairedLedger(path, run_id="another-id")
    GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
    )
    ledger.limits["llm"] = 181
    with pytest.raises(ValueError):
        GuardedTransport(
            provider="llm",
            endpoint="https://example.org/chat/completions",
            ledger=ledger,
            output=tmp_path,
            secrets=(),
            max_output_tokens=8192,
        )


@pytest.mark.parametrize("changed", ["model", "endpoint", "profile", "history", "contract"])
@pytest.mark.parametrize("version", [11, 12, 13, 14])
async def test_v11_changed_configuration_blocks_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str, version: int
) -> None:
    path = prepare(tmp_path, monkeypatch, version)
    configuration = configured_settings()
    configuration.llm_compatibility_profile = "deepseek"
    monkeypatch.setattr(paired, "load_explicit_settings", lambda *a, **k: (configuration, ""))
    if changed == "model":
        configuration.llm_model = "changed-model"
    elif changed == "endpoint":
        configuration.llm_base_url = "https://other.example/v1"
    elif changed == "profile":
        configuration.llm_compatibility_profile = "generic"
    elif changed == "history":
        prior = {11: "live-baseline-v10-claim.json", 12: CLAIM, 13: RETRY_CLAIM, 14: FINAL_CLAIM}[
            version
        ]
        (tmp_path / "data/evaluation" / prior).write_text("{}")
    else:
        value = json.loads(path.read_text())
        value["contract"]["physical_limits"]["llm"] = 181
        path.write_text(json.dumps(value))
    args = argparse.Namespace(
        allow_paid=True,
        protocol_version=version,
        plan=path,
        approval_id="offline-v11-only",
        sandbox_container="never-contacted",
    )
    with pytest.raises(ValueError):
        await paired.run_paired(args, repo_root=tmp_path)
    claim = {11: CLAIM, 12: RETRY_CLAIM, 13: FINAL_CLAIM, 14: DAG_CLAIM}[version]
    assert not (tmp_path / "data/evaluation" / claim).exists()
    assert not (tmp_path / "data/evaluation/provider-approvals").exists()


@pytest.mark.parametrize("version", [11, 12, 13, 14])
def test_v11_protects_v10_without_reusing_its_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    path = prepare(tmp_path, monkeypatch, version)
    value = paired.load_paired_plan(path, tmp_path, version)
    assert value["contract"] == repaired_paired_contract(version)
    assert value["contract"]["tasks"] == list(TASKS)
    assert value["contract"]["historical_observations_reused"] is False
    assert "test-secret" not in path.read_text() and "llm.example" not in path.read_text()
    expected = f"consumed v{version - 1}"
    with pytest.raises(ValueError, match=expected):
        paired.prepare_paired(tmp_path, history(tmp_path, version - 1), version)
    claim = {11: CLAIM, 12: RETRY_CLAIM, 13: FINAL_CLAIM, 14: DAG_CLAIM}[version]
    assert not (tmp_path / "data/evaluation" / claim).exists()
