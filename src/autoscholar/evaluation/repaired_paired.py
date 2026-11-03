"""New repaired-runtime paired scope; no historical grant or observation is reused."""

from pathlib import Path
from typing import Any

from autoscholar.evaluation.baseline_protocol import baseline_contract
from autoscholar.evaluation.paired_protocol import PairedLedger, paired_contract, paired_summary
from autoscholar.evaluation.runner import _write_json

CLAIM = "live-paired-v11-claim.json"
SCOPE = "phase10_repaired_paired_v11"


def repaired_paired_contract() -> dict[str, Any]:
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
    return contract


class RepairedPairedLedger(PairedLedger):
    def __init__(self, path: Path, *, run_id: str) -> None:
        super().__init__(path, run_id=run_id)
        self.data.update(protocol_version=11, scope=SCOPE)
        _write_json(path, self.data)


def repaired_paired_summary(
    records: list[dict[str, Any]], ledger: PairedLedger, *, clean: bool, unchanged: bool
) -> dict[str, Any]:
    summary = paired_summary(records, ledger, clean=clean, unchanged=unchanged)
    complete = bool(
        summary["paired_measurement_complete"]
        and ledger.data.get("protocol_version") == 11
        and ledger.data.get("scope") == SCOPE
        and ledger.limits == {"llm": 180, "tavily": 0}
        and all(row.get("execution_gates_passed") is True for row in records)
    )
    summary.update(
        protocol_version=11,
        paired_measurement_complete=complete,
        live_pilot_measurement_complete=complete,
        paired_pilot_acceptance=bool(complete and summary["paired_pilot_acceptance"]),
        historical_observations_reused=False,
        scope=repaired_paired_contract()["limitations"],
    )
    return summary
