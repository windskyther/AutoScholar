"""Bind completed manual evidence for an explicitly approved two-request tool check."""

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import MeasuredUsage
from autoscholar.evaluation.provider_cases import (
    CASE_IDS,
    CASES,
    oracle_digest,
    public_suite_digest,
)
from autoscholar.evaluation.provider_prior import (
    PriorPlanning,
    load_stopped_followup,
    read_public,
    same_grader_source,
)
from autoscholar.evaluation.provider_transport import approval_path


@dataclass
class ToolConfirmationEvidence:
    directory: Path
    prior: PriorPlanning
    records: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    digests: dict[Path, str]

    def unchanged(self) -> bool:
        try:
            return self.prior.unchanged() and all(
                read_public(path, path.parent)[1] == digest for path, digest in self.digests.items()
            )
        except (OSError, ValueError):
            return False

    def binding(self) -> dict[str, Any]:
        root = self.directory.parent
        return {
            "run_id": self.directory.name,
            "file_sha256": {
                path.relative_to(root).as_posix(): sha for path, sha in self.digests.items()
            },
            "previous_attempts": self.attempts,
            "original_failed_results_preserved": True,
        }


def load_tool_confirmation(
    directory: Path, *, repo_root: Path, prior: PriorPlanning, model: str | None, endpoint: str
) -> ToolConfirmationEvidence:
    root = (repo_root / "data/evaluation").resolve()
    directory = directory.resolve()
    if directory.parent != root or not re.fullmatch(r"provider-[a-f0-9]{32}", directory.name):
        raise ValueError("Select an exact completed public continuation for tool confirmation")
    snapshots: dict[str, Any] = {}
    digests: dict[Path, str] = {}

    def capture(path: Path) -> Any:
        value, digests[path] = read_public(path, root)
        return value

    paths = sorted(directory.glob("*.json"))
    if len(paths) > 40:
        raise ValueError("Too many public confirmation snapshots")
    for path in paths:
        snapshots[path.name] = capture(path)
    manifest = snapshots["manifest.json"]
    records = snapshots["cases.json"]
    ledger = snapshots["request_ledger.json"]
    revision = manifest.get("git_revision", "")
    if (
        manifest.get("run_id") != directory.name
        or manifest.get("state") != "completed"
        or manifest.get("git_dirty") is not False
        or not isinstance(revision, str)
        or not re.fullmatch(r"[a-f0-9]{40}", revision)
        or manifest.get("configured_model") != model
        or manifest.get("endpoint_sha256") != hashlib.sha256(endpoint.encode()).hexdigest()
        or manifest.get("suite_sha256") != public_suite_digest()
        or manifest.get("oracle_sha256") != oracle_digest()
        or manifest.get("prior_evidence") != prior.binding()
        or manifest.get("request_limits") != {"llm": 9, "tavily": 2}
        or not same_grader_source(repo_root, revision)
        or tuple(row.get("case_id") for row in records) != CASE_IDS
        or records[0] != prior.record
        or records[1].get("status") != "error"
        or records[2].get("status") != "skipped"
    ):
        raise ValueError("Completed continuation source, rubric or case binding differs")
    stopped_id = manifest.get("continued_from", "")
    if not isinstance(stopped_id, str) or not re.fullmatch(r"provider-[a-f0-9]{32}", stopped_id):
        raise ValueError("Invalid stopped follow-up identity")
    stopped = load_stopped_followup(
        root / stopped_id,
        repo_root=repo_root,
        prior=prior,
        model=model,
        endpoint=endpoint,
        identifier=manifest["approval_id"],
    )
    for name in (
        "manifest.json",
        "cases.json",
        "request_ledger.json",
        "request-llm-native_tool_call.json",
    ):
        capture(stopped["directory"] / name)
    claim = capture(root / "provider-continuation-claims" / (stopped_id + ".json"))
    capture(root / "provider-followup-claims" / (prior.directory.name + ".json"))
    grant = capture(approval_path(root, manifest["approval_id"]))
    attempts = ledger.get("attempts", [])
    expected = [("llm", "native_tool_call")]
    for case in CASES[3:]:
        if case.kind == "research":
            expected.append(("tavily", case.id))
        expected.append(("llm", case.id))
    if (
        claim != {"from_run": stopped_id, "run_id": directory.name}
        or grant != ledger
        or ledger.get("run_id") != stopped_id
        or ledger.get("limits") != {"llm": 9, "tavily": 2}
        or ledger.get("automatic_retries") is not False
        or ledger.get("blocked_case_ids") != ["planning"]
        or [(row.get("provider"), row.get("case_id")) for row in attempts] != expected
        or attempts[:1] != stopped["ledger"]["attempts"]
        or records[:2] != stopped["records"]
        or any(
            row.get("state") != "returned" or row.get("http_status") != 200 for row in attempts[1:]
        )
    ):
        raise ValueError("Consumed grants or continuation ownership differ; never reset counters")
    for row in attempts[1:]:
        MeasuredUsage.model_validate(row["usage"])
        request = snapshots[f"request-{row['provider']}-{row['case_id']}.json"]
        if payload_digest(request) != row["request_sha256"]:
            raise ValueError("Previous request-body hash differs")
    for case, record in zip(CASES[3:], records[3:], strict=True):
        output = snapshots[case.id + "-response.json"]
        if record.get("input_sha256") != payload_digest(asdict(case)) or (
            record.get("status") == "passed"
            and record.get("output_sha256") != payload_digest(output)
        ):
            raise ValueError("Previous case request/response binding differs")
    return ToolConfirmationEvidence(
        directory,
        prior,
        records,
        prior.snapshots["request_ledger.json"]["attempts"] + attempts,
        digests,
    )
