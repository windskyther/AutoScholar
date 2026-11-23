"""Provider-frozen one-shot v10 admission and budget tests; no external requests."""

import argparse
import json
from pathlib import Path

import pytest

from autoscholar.evaluation import paired_validation as paired
from autoscholar.evaluation.baseline_protocol import (
    COMPATIBILITY_CLAIM,
    BaselineLedger,
    baseline_contract,
)
from autoscholar.evaluation.provider_transport import GuardedTransport
from tests.test_llm_provider import configured_settings
from tests.test_paired_v8 import history, prepare


@pytest.mark.parametrize("changed", ["model", "endpoint", "profile", "history", "contract"])
async def test_changed_provider_or_history_blocks_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    plan = prepare(tmp_path, monkeypatch, 10)
    settings = configured_settings()
    settings.llm_compatibility_profile = "deepseek"
    monkeypatch.setattr(paired, "load_explicit_settings", lambda *a, **k: (settings, ""))
    if changed == "model":
        settings.llm_model = "different-model"
    elif changed == "endpoint":
        settings.llm_base_url = "https://other.example/v1"
    elif changed == "profile":
        settings.llm_compatibility_profile = "generic"
    elif changed == "history":
        (tmp_path / "data/evaluation/live-baseline-v9-claim.json").write_text("{}")
    else:
        payload = json.loads(plan.read_text())
        payload["contract"]["physical_limits"]["llm"] = 13
        plan.write_text(json.dumps(payload))
    args = argparse.Namespace(
        allow_paid=True,
        plan=plan,
        protocol_version=10,
        approval_id="offline-only",
        sandbox_container="never-contacted",
    )
    with pytest.raises(ValueError):
        await paired.run_paired(args, repo_root=tmp_path)
    assert not (tmp_path / "data/evaluation" / COMPATIBILITY_CLAIM).exists()
    assert not (tmp_path / "data/evaluation/provider-approvals").exists()


def test_prepare_requires_v9_history_and_records_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = prepare(tmp_path, monkeypatch, 10)
    value = paired.load_paired_plan(path, tmp_path, 10)
    assert value["contract"] == baseline_contract(10)
    assert value["provider_binding"]["effective_profile"] == "deepseek"
    assert "test-secret" not in path.read_text()
    assert "llm.example" not in path.read_text()  # endpoint is bound only by digest
    assert value["paid_execution_approved"] is False
    with pytest.raises(ValueError, match="consumed v9"):
        paired.prepare_paired(tmp_path, history(tmp_path, 9), 10)
    assert not (tmp_path / "data/evaluation" / COMPATIBILITY_CLAIM).exists()


def test_v10_fixed_task_allowance_and_no_reset(tmp_path: Path) -> None:
    path = tmp_path / "mock-ledger.json"
    ledger = BaselineLedger(path, run_id="mock-only", protocol_version=10)
    assert ledger.limits == {"llm": 12, "tavily": 0}
    for provider, task in (("tavily", "normal-baseline"), ("llm", "recover-baseline")):
        with pytest.raises(ValueError):
            ledger.reserve(provider, task + "--1", {})  # type: ignore[arg-type]
    for ordinal in range(1, 13):
        ledger.reserve("llm", f"normal-baseline--{ordinal}", {})
    with pytest.raises(ValueError):
        ledger.reserve("llm", "normal-baseline--13", {})
    with pytest.raises(FileExistsError):
        BaselineLedger(path, run_id="another-id", protocol_version=10)
    assert len(ledger.attempts) == 12
    GuardedTransport(
        provider="llm",
        endpoint="https://example.org/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
    )
    ledger.data["scope"] = "phase10_output_baseline_v9"
    with pytest.raises(ValueError):
        GuardedTransport(
            provider="llm",
            endpoint="https://example.org/chat/completions",
            ledger=ledger,
            output=tmp_path,
            secrets=(),
            max_output_tokens=8192,
        )
