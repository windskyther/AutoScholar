"""New repaired-runtime paired scope; no historical grant or observation is reused."""

from pathlib import Path
from typing import Any

from autoscholar.evaluation.baseline_protocol import baseline_contract
from autoscholar.evaluation.paired_protocol import PairedLedger, paired_contract, paired_summary
from autoscholar.evaluation.runner import _write_json

CLAIM = "live-paired-v11-claim.json"
SCOPE = "phase10_repaired_paired_v11"
RETRY_CLAIM = "live-paired-v12-claim.json"
RETRY_SCOPE = "phase10_repaired_retry_v12"
FINAL_CLAIM = "live-paired-v13-claim.json"
FINAL_SCOPE = "phase10_final_paired_v13"
DAG_CLAIM = "live-paired-v14-claim.json"
DAG_SCOPE = "phase10_fixed_dag_paired_v14"


def repaired_paired_contract(version: int = 11) -> dict[str, Any]:
    if type(version) is not int or version not in (11, 12, 13, 14):
        raise ValueError("Only repaired paired versions 11 through 14 supported")
    contract = paired_contract()
    repaired = baseline_contract(10)
    contract.update(
        protocol_version=11,
        scope=SCOPE,
        resources=repaired["resources"],
        compatibility=repaired["compatibility"],
        snapshot_output=repaired["snapshot_output"],
        historical_observations_reused=False,
        limitations=(
            "New 15-task seeded-code CPU/MNIST/closed-corpus paired engineering pilot on the "
            "repaired provider runtime. Three heterogeneous paired units per contrast; "
            "no powered component-benefit claim. v10 normal baseline stays historical. "
            "No task replay, old grant reuse or full Phase 10 acceptance."
        ),
    )
    if version == 12:
        contract.update(
            protocol_version=12,
            scope=RETRY_SCOPE,
            previous_attempt_scope=SCOPE,
            retry_requires_new_user_instruction=True,
            limitations=(
                "One explicitly requested new 15-task attempt after v11 provider interruption. "
                "Same repaired runtime and 180/0 ceiling; historical grants and observations "
                "stay separate. Three heterogeneous pairs per contrast; no powered benefit "
                "claim or full Phase 10 acceptance. No automatic whole-task retry."
            ),
        )
    if version in (13, 14):
        contract.update(
            protocol_version=13,
            scope=FINAL_SCOPE,
            previous_attempt_scope=RETRY_SCOPE,
            coding_schema_feedback={
                "snapshot_only": True,
                "complete_local_schema_rejection_only": True,
                "known_consistent_usage_and_physical_receipt_required": True,
                "shares_one_task_correction_with_coordinator": True,
                "successful_edit_does_not_reset_correction": True,
                "rejected_batch_executes_no_tools": True,
                "existing_call_token_and_reserve_limits_preserved": True,
            },
            limitations=(
                "One authorized 15-task final paired engineering run after the bounded Coding "
                "schema-feedback fix; no separate paid warmup. All observations are new on "
                "one frozen source. Same 180/0 ceiling and 12 per task. Three heterogeneous "
                "pairs per contrast, no powered component-benefit claim. Overall Phase 10 "
                "acceptance requires the separate final evidence and integration audit. "
                "No old grant reuse or automatic whole-task retry."
            ),
        )
    if version == 14:
        contract.update(
            protocol_version=14,
            scope=DAG_SCOPE,
            previous_attempt_scope=FINAL_SCOPE,
            root_dag_admission={
                "roles": ["research", "coding", "experiment"],
                "linear_dependencies": True,
                "before_any_child_execution": True,
                "wrong_plan_is_not_rewritten": True,
                "shares_existing_one_metered_correction": True,
            },
            limitations=(
                "One authorized new 15-task paired run after fixed-DAG admission repair. "
                "Same runtime, 180/0 ceiling and 12 per task; no paid warmup or old observation "
                "reuse. Three heterogeneous pairs per contrast, no powered benefit claim. "
                "Overall Phase 10 acceptance requires final evidence and integration audit. "
                "No old grant reuse or automatic whole-task retry."
            ),
        )
    return contract


class RepairedPairedLedger(PairedLedger):
    def __init__(self, path: Path, *, run_id: str, protocol_version: int = 11) -> None:
        contract = repaired_paired_contract(protocol_version)
        super().__init__(path, run_id=run_id)
        self.data.update(protocol_version=protocol_version, scope=contract["scope"])
        _write_json(path, self.data)


def repaired_paired_summary(
    records: list[dict[str, Any]], ledger: PairedLedger, *, clean: bool, unchanged: bool
) -> dict[str, Any]:
    summary = paired_summary(records, ledger, clean=clean, unchanged=unchanged)
    version = ledger.data.get("protocol_version")
    if type(version) is not int:
        raise ValueError("Missing repaired paired protocol version")
    contract = repaired_paired_contract(version)
    complete = bool(
        summary["paired_measurement_complete"]
        and ledger.data.get("scope") == contract["scope"]
        and ledger.limits == {"llm": 180, "tavily": 0}
        and all(row.get("execution_gates_passed") is True for row in records)
    )
    summary.update(
        protocol_version=version,
        paired_measurement_complete=complete,
        live_pilot_measurement_complete=complete,
        paired_pilot_acceptance=bool(complete and summary["paired_pilot_acceptance"]),
        historical_observations_reused=False,
        scope=contract["limitations"],
    )
    return summary
