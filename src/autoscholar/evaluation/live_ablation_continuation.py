"""One explicitly approved continuation: seven untouched tasks, never replay old work."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from autoscholar.evaluation.datasets import payload_digest, read_bounded
from autoscholar.evaluation.live_ablation_contract import LIMITS, TASKS, LiveAblationLedger
from autoscholar.evaluation.live_ablation_v2 import OUTPUT_TOKENS, SUFFIX, PriorPilot
from autoscholar.evaluation.provider_prior import read_public
from autoscholar.evaluation.provider_transport import Provider, approval_path

CONTINUATION_TASKS = TASKS[8:]
NEW_REQUEST_LIMIT = 84
HISTORICAL_REQUESTS = 85
CLAIM_NAME = "live-ablation-v2-continuation-claim.json"
V2_APPROVAL = "phase10-live-ablation-v2-20261016"


class StoppedV2:
    def __init__(self, directory: Path, prior: PriorPilot) -> None:
        self.root = prior.root
        self.directory = directory.resolve()
        if self.directory.parent != self.root or not re.fullmatch(
            r"live-ablation-v2-[a-f0-9]{32}", self.directory.name
        ):
            raise ValueError("Select the exact stopped v2 pilot")
        self.hashes = dict(prior.hashes)

        def read(path: Path) -> Any:
            value, sha = read_public(path, self.root)
            self.hashes[path.relative_to(self.root).as_posix()] = sha
            return value

        self.manifest = read(self.directory / "manifest.json")
        self.records = read(self.directory / "cases.json")
        self.acceptance = read(self.directory / "acceptance.json")
        claim = read(self.root / "live-ablation-v2-claim.json")
        ledger = read(approval_path(self.root, V2_APPROVAL))
        snapshot = read(self.directory / "request_ledger.json")
        if (
            ledger != snapshot
            or ledger.get("limits") != {"llm": 159, "tavily": 0}
            or ledger.get("run_id") != self.directory.name
            or claim.get("run_id") != self.directory.name
            or claim.get("approval_file") != V2_APPROVAL + ".json"
            or claim.get("primary_sha256") != prior.hashes
            or self.manifest.get("prior_primary_sha256") != prior.hashes
            or self.manifest.get("configured_model") != prior.manifest["configured_model"]
            or self.manifest.get("protocol_version") != 2
            or self.manifest.get("objective_suffix") != SUFFIX
            or self.manifest.get("limits") != LIMITS.model_dump()
            or self.manifest.get("max_output_tokens") != OUTPUT_TOKENS
            or self.manifest.get("tasks") != list(TASKS)
            or [row["task"] for row in self.records] != list(TASKS[:8])
            or self.acceptance.get("live_pilot_measurement_complete") is not False
        ):
            raise ValueError("Stopped v2 identity/protocol changed")
        attempts = ledger.get("attempts", [])
        if len(attempts) != 64:
            raise ValueError("Expected exactly 64 prior v2 physical requests")
        counts: dict[str, int] = {}
        usage: dict[str, dict[str, int]] = {}
        for attempt in attempts:
            identifier = attempt["case_id"]
            task, _, ordinal = identifier.rpartition("--")
            counts[task] = counts.get(task, 0) + 1
            if (
                task not in TASKS[:8]
                or ordinal != str(counts[task])
                or counts[task] > 12
                or attempt["provider"] != "llm"
                or attempt["state"] != "returned"
                or attempt["http_status"] != 200
            ):
                raise ValueError("Unresolved or invalid historical physical request")
            request = read(self.directory / f"request-llm-{identifier}.json")
            response = read(self.directory / f"response-llm-{identifier}.json")
            if (
                payload_digest(request) != attempt["request_sha256"]
                or request.get("max_tokens") != OUTPUT_TOKENS
                or request.get("model") != self.manifest["configured_model"]
            ):
                raise ValueError("Historical request differs")
            for key, vendor in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = attempt["usage"].get(key)
                if (
                    type(value) is not int
                    or value < 0
                    or value != response.get("usage", {}).get(vendor)
                ):
                    raise ValueError("Historical token receipt differs or is unknown")
                bucket = usage.setdefault(task, {})
                bucket[key] = bucket.get(key, 0) + value
        for row in self.records:
            task = row["task"]
            read(self.directory / task / "workflow.json")
            if row.get("physical_requests") != counts.get(task) or any(
                row.get("usage", {}).get(key) != value for key, value in usage[task].items()
            ):
                raise ValueError("Historical task usage differs from receipts")
            if task != TASKS[7] and not (
                row.get("status") == "measured"
                and row.get("physical_accounting_verified") is True
                and row.get("usage", {}).get("model_calls") == counts[task]
            ):
                raise ValueError("Earlier task is not a closed measurement")
        last = self.records[-1]
        if not (
            last.get("status") == "provider_error"
            and last.get("task_success") is None
            and counts[TASKS[7]] == 8
            and last.get("usage", {}).get("model_calls") == 9
        ):
            raise ValueError("Expected audited terminal task; never resume it")
        self.vendor_tokens = sum(row["total_tokens"] for row in usage.values())
        if self.acceptance.get("vendor_tokens") != self.vendor_tokens:
            raise ValueError("Historical total differs")
        # The additive audit binds even primary files not covered by a request hash.
        audit, sha = read_public(self.directory / "audit-20261021.json", self.root)
        if audit.get("primary_sha256") != self.hashes:
            raise ValueError("Previously audited evidence changed")
        self.hashes[(self.directory / "audit-20261021.json").relative_to(self.root).as_posix()] = (
            sha
        )
        if not self.unchanged():
            raise ValueError("Historical evidence changed during validation")

    def unchanged(self) -> bool:
        return all(
            hashlib.sha256(read_bounded(self.root / name)).hexdigest() == sha
            for name, sha in self.hashes.items()
        )


class ContinuationLedger(LiveAblationLedger):
    def __init__(
        self, path: Path, *, run_id: str, stopped: StoppedV2, manifest: dict[str, Any]
    ) -> None:
        self.stopped = stopped
        if path.exists() or not stopped.unchanged():
            raise ValueError("Existing allowance or changed evidence; no reset")
        with (stopped.root / CLAIM_NAME).open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "run_id": run_id,
                    "approval_file": path.name,
                    "manifest_sha256": payload_digest(manifest),
                    "prior_requests": HISTORICAL_REQUESTS,
                    "new_request_limit": NEW_REQUEST_LIMIT,
                    "tasks": list(CONTINUATION_TASKS),
                    "primary_sha256": stopped.hashes,
                },
                stream,
            )
        self.path = path
        self.limits = {"llm": NEW_REQUEST_LIMIT, "tavily": 0}
        self.blocked_case_ids = ()
        self.data = {
            "schema_version": 1,
            "protocol_version": 2,
            "continuation_version": 1,
            "run_id": run_id,
            "limits": self.limits,
            "tasks": list(CONTINUATION_TASKS),
            "prior_requests": HISTORICAL_REQUESTS,
            "cumulative_limit": 180,
            "authorized_cumulative_limit": 169,
            "per_task_llm_limit": 12,
            "prior_primary_sha256": stopped.hashes,
            "attempts": [],
            "automatic_retries": False,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, allow_nan=False)

    def reserve(self, provider: Provider, case_id: str, payload: dict[str, Any]) -> int:
        if case_id.rpartition("--")[0] not in CONTINUATION_TASKS:
            raise ValueError("Continuation forbids every previously attempted task")
        if not self.stopped.unchanged():
            raise ValueError("Historical evidence changed; stop continuation")
        return super().reserve(provider, case_id, payload)
