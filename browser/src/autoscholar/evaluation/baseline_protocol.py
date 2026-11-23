"""One fresh normal baseline after bounded-output repair; old grants stay consumed."""

import copy
from pathlib import Path
from typing import Any

from autoscholar.coding.snapshot import MAX_ARGUMENT_BYTES, TEXT_LIMITS
from autoscholar.evaluation.live_ablation import pilot_summary
from autoscholar.evaluation.paired_protocol import baseline_passed
from autoscholar.evaluation.repair_protocol import RepairLedger, repair_contract
from autoscholar.evaluation.runner import _write_json

CLAIM = "live-baseline-v9-claim.json"
SCOPE = "phase10_output_baseline_v9"
COMPATIBILITY_CLAIM = "live-baseline-v10-claim.json"
COMPATIBILITY_SCOPE = "phase10_compatibility_baseline_v10"
RESOURCES = {
    "sandbox_image": "5f8f4de620311dfe4303b679048b9eced521ddf3201c5d743ae212b16e2c0e3a",
    "dataset": "f4d6a18adf158439195f76a1267a7918723faf6ae121eff9914ed83afc53c476",
}


def baseline_contract(version: int = 9) -> dict[str, Any]:
    if type(version) is not int or version not in (9, 10):
        raise ValueError("Only single baseline protocols 9 and 10 supported")
    contract = repair_contract(7)
    contract.update(
        protocol_version=9,
        scope=SCOPE,
        tasks=["normal-baseline"],
        resources=dict(RESOURCES),
        snapshot_output={
            "max_argument_utf8_bytes": MAX_ARGUMENT_BYTES,
            "text_limits": copy.deepcopy(TEXT_LIMITS),
            "truncated_native_output": "reject entire completion; charge usage; no retry",
        },
        acceptance={
            "normal-baseline": "One research/code/train step; checks and independent oracle pass",
            "all": "Known usage, one observed model, unchanged source/history, clean resources",
        },
        limitations=(
            "One new seeded-code normal baseline only; no fault injection or ablation. "
            "No whole-task replay or reuse of old grants. Not Phase 10 overall acceptance."
        ),
    )
    if version == 10:
        contract.update(
            protocol_version=10,
            scope=COMPATIBILITY_SCOPE,
            compatibility={
                "effective_profile": "deepseek",
                "forced_tools_disable_thinking": True,
                "unsupported_bounds_checked_against_original_local_schema": True,
                "provider_endpoint_model_and_profile_bound_before_dispatch": True,
                "non_200_categorical_diagnostics": True,
            },
            limitations=(
                "One new normal baseline on the repaired DeepSeek compatibility runtime. "
                "Provider binding frozen before approval. No fault injection or ablation, "
                "no replay or reuse of v9 allowance. Not Phase 10 overall acceptance."
            ),
        )
    return contract


class BaselineLedger(RepairLedger):
    def __init__(self, path: Path, *, run_id: str, protocol_version: int = 9) -> None:
        contract = baseline_contract(protocol_version)
        super().__init__(path, run_id=run_id, protocol_version=7)
        self.data.update(
            protocol_version=protocol_version, scope=contract["scope"], tasks=["normal-baseline"]
        )
        _write_json(path, self.data)


def baseline_summary(
    records: list[dict[str, Any]], ledger: BaselineLedger, *, clean: bool, unchanged: bool
) -> dict[str, Any]:
    summary = pilot_summary(records, ledger, clean=clean, unchanged=unchanged)
    accepted = bool(
        len(records) == 1
        and records[0].get("task") == "normal-baseline"
        and records[0].get("execution_gates_passed") is True
        and baseline_passed(records[0])
        and all(records[0].get("checks", {}).values())
        and records[0].get("checks", {}).get("independent_checkpoint_oracle") is True
        and records[0].get("physical_accounting_verified") is True
        and records[0].get("intervention_verified") is True
        and len(records[0].get("observed_models", [])) == 1
        and 0 < len(ledger.attempts) <= 12
        and clean
        and unchanged
    )
    summary.update(
        protocol_version=ledger.data["protocol_version"],
        planned_tasks=1,
        baseline_acceptance=accepted,
        phase10_overall_acceptance=False,
        statistical_benefit_claim=False,
        scope=baseline_contract(ledger.data["protocol_version"])["limitations"],
    )
    return summary
