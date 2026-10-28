"""Fresh, source-bound v8 paired pilot; consumed historical grants are never reused."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoscholar.evaluation.datasets import read_bounded
from autoscholar.evaluation.live_ablation import pilot_summary
from autoscholar.evaluation.live_ablation_contract import TASKS, LiveAblationLedger
from autoscholar.evaluation.provider_prior import read_public
from autoscholar.evaluation.repair_protocol import repair_contract
from autoscholar.evaluation.runner import _write_json

SCOPE = "phase10_paired_workflow_v8"
CLAIM = "live-paired-v8-claim.json"
HISTORICAL_CLAIMS = (
    "live-ablation-v1-claim.json",
    "live-ablation-v2-claim.json",
    "live-ablation-v2-continuation-claim.json",
    *(f"live-repair-v{version}-claim.json" for version in range(3, 8)),
)


def paired_contract() -> dict[str, Any]:
    contract = repair_contract(7)
    contract.update(
        protocol_version=8,
        scope=SCOPE,
        tasks=list(TASKS),
        physical_limits={"llm": 180, "tavily": 0, "per_task_llm": 12},
        acceptance={
            "measurement": "All 15 identity-bound cases and four complete contrasts",
            "controls": (
                "Normal/recovery baselines pass; deceptive baseline reaches rejecting oracle; "
                "recovery No Replanning reaches fault and stops"
            ),
            "stop_gates": (
                "Stop on failed scenario baseline, provider/accounting/resource fault "
                "or changed source/history/model"
            ),
        },
        statistics={
            "paired_units_per_contrast": 3,
            "direction": "variant_minus_baseline",
            "test": "two-sided exact paired binary sign test; Holm across four contrasts",
            "significance_claim": False,
        },
        limitations=(
            "New 15-task seeded-code CPU/MNIST/closed-public-corpus paired engineering pilot. "
            "One attempt per task, three heterogeneous paired units per contrast; no powered "
            "component-benefit claim. Historical versions stay separate. "
            "Not full Phase 10 acceptance."
        ),
    )
    return contract


class PairedLedger(LiveAblationLedger):
    def __init__(self, path: Path, *, run_id: str) -> None:
        super().__init__(path, run_id=run_id)
        self.data.update(protocol_version=8, scope=SCOPE)
        _write_json(path, self.data)


def history_snapshot(path: Path, root: Path) -> tuple[dict[str, str], dict[str, str]]:
    index, digest = read_public(path.resolve(), root)
    if not isinstance(index, dict) or index.get("schema_version") != 1:
        raise ValueError("Invalid historical index")
    shards = index.get("shards")
    if not isinstance(shards, list) or not 1 <= len(shards) <= 16:
        raise ValueError("Expected 1..16 bounded historical shards")
    protected: dict[str, str] = {}
    for shard in shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("path"), str):
            raise ValueError("Invalid historical shard")
        rows, actual = read_public(root / shard["path"], root)
        if actual != shard.get("sha256") or not isinstance(rows, dict):
            raise ValueError("Historical shard changed")
        if protected.keys() & rows.keys() or len(protected) + len(rows) > 2048:
            raise ValueError("Duplicate or excessive historical bindings")
        protected.update(rows)
    if not set(HISTORICAL_CLAIMS) <= protected.keys():
        raise ValueError("Retain every consumed historical claim")
    for name, expected in protected.items():
        if (
            not isinstance(name, str)
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
        ):
            raise ValueError("Invalid historical file binding")
        target = (root / name).resolve()
        if (
            not target.is_relative_to(root.resolve())
            or hashlib.sha256(read_bounded(target)).hexdigest() != expected
        ):
            raise ValueError("Historical evidence changed; no dispatch")
    return protected, {
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "sha256": digest,
    }


def write_history(root: Path, protected: dict[str, str]) -> Path:
    """Shard a complete local map without widening the 128 KiB public read bound."""
    directory = root / ("paired-history-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    buckets: list[dict[str, str]] = [{}]
    for name, digest in sorted(protected.items()):
        candidate = {**buckets[-1], name: digest}
        if len(json.dumps(candidate).encode()) > 64000:
            buckets.append({name: digest})
        else:
            buckets[-1] = candidate
    shards = []
    for ordinal, bucket in enumerate(buckets):
        path = directory / f"files-{ordinal}.json"
        _write_json(path, bucket)
        shards.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(read_bounded(path)).hexdigest(),
            }
        )
    path = directory / "index.json"
    _write_json(path, {"schema_version": 1, "shards": shards})
    history_snapshot(path, root)
    return path


def baseline_passed(row: dict[str, Any]) -> bool:
    scenario, _, variant = row["task"].partition("-")
    if variant != "baseline":
        return True
    expected_train = 2 if scenario == "recover" else 1
    return bool(
        row.get("status") == "measured"
        and row.get("checks", {}).get("code_validated")
        and row.get("step_counts") == {"research": 1, "code": 1, "train": expected_train}
        and row.get("usage", {}).get("training_runs") == expected_train
        and row.get("fault_mutations") == (0 if scenario == "normal" else 1)
        and (
            row.get("task_success") is True
            if scenario != "deception"
            else row.get("task_success") is False
            and row.get("verification") is not None
            and row.get("checks", {}).get("independent_checkpoint_oracle") is False
        )
    )


def paired_summary(
    records: list[dict[str, Any]], ledger: LiveAblationLedger, *, clean: bool, unchanged: bool
) -> dict[str, Any]:
    summary = pilot_summary(records, ledger, clean=clean, unchanged=unchanged)
    identities = {tuple(row.get("observed_models", [])) for row in records}
    complete = bool(
        summary["live_pilot_measurement_complete"]
        and [row["task"] for row in records] == list(TASKS)
        and len(identities) == 1
        and all(len(identity) == 1 for identity in identities)
        and sum(row.get("physical_requests", 0) for row in records) == len(ledger.attempts)
    )
    controls = {
        row["task"]: baseline_passed(row) for row in records if row["task"].endswith("-baseline")
    }
    negative = next((row for row in records if row["task"] == "recover-no_replanning"), {})
    negative_ok = bool(
        negative.get("task_success") is False
        and negative.get("error_code") == "evaluation_replanning_disabled"
        and negative.get("fault_mutations") == 1
        and negative.get("checks", {}).get("code_validated")
        and negative.get("step_counts") == {"research": 1, "code": 1, "train": 1}
        and negative.get("usage", {}).get("training_runs") == 1
    )
    # Old summary contracts assumed other ablations should succeed; retain observations,
    # but judge controls separately instead of turning removed capabilities into failures.
    summary.pop("scenario_contracts")
    summary.update(
        protocol_version=8,
        paired_measurement_complete=complete,
        live_pilot_measurement_complete=complete,
        baseline_controls=controls,
        negative_control_passed=negative_ok,
        paired_pilot_acceptance=bool(complete and all(controls.values()) and negative_ok),
        statistical_benefit_claim=False,
        scope=paired_contract()["limitations"],
    )
    return summary
