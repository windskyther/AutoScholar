"""Bound exact prior planning evidence; no API, private files, or allowance reset."""

import hashlib
import re
import subprocess
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoscholar.evaluation.datasets import decode_json, payload_digest
from autoscholar.evaluation.models import MeasuredUsage
from autoscholar.evaluation.provider_cases import (
    CASES,
    SYSTEM,
    grade_structured,
    oracle_digest,
    parse_answer,
    public_suite_digest,
)
from autoscholar.evaluation.provider_transport import LIMITS, MAX_OUTPUT_TOKENS, approval_path

FILES = (
    "manifest.json",
    "cases.json",
    "request_ledger.json",
    "planning-response.json",
    "request-llm-planning.json",
    "request-llm-native_tool_call.json",
)


def read_public(path: Path, root: Path) -> tuple[Any, str]:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Prior evidence escaped its public run directory")
    with path.open("rb") as stream:
        raw = stream.read(131073)
    if not raw or len(raw) > 131072:
        raise ValueError("Prior evidence exceeds bounded public snapshot size")
    return decode_json(raw), hashlib.sha256(raw).hexdigest()


def same_grader_source(repo_root: Path, revision: str) -> bool:
    result = subprocess.run(
        ["git", "show", revision + ":src/autoscholar/evaluation/provider_cases.py"],
        cwd=repo_root,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=5,
    )
    return result.returncode == 0 and result.stdout == Path(__file__).with_name(
        "provider_cases.py"
    ).read_text(encoding="utf-8")


@dataclass
class PriorPlanning:
    directory: Path
    record: dict[str, Any]
    snapshots: dict[str, Any]
    digests: dict[str, str]
    approval_file: Path

    def unchanged(self) -> bool:
        try:
            return (
                all(
                    read_public(self.directory / name, self.directory)[1] == digest
                    for name, digest in self.digests.items()
                    if name != "approval_ledger"
                )
                and read_public(self.approval_file, self.approval_file.parent)[1]
                == self.digests["approval_ledger"]
            )
        except (OSError, ValueError):
            return False

    def binding(self) -> dict[str, Any]:
        return {
            "run_id": self.directory.name,
            "git_revision": self.snapshots["manifest.json"]["git_revision"],
            "file_sha256": self.digests,
            "reused_case_ids": ["planning"],
            "prior_attempts": self.snapshots["request_ledger.json"]["attempts"],
        }


def load_prior(
    directory: Path, *, repo_root: Path, model: str | None, endpoint: str
) -> PriorPlanning:
    root = (repo_root / "data/evaluation").resolve()
    directory = directory.resolve()
    if directory.parent != root or not re.fullmatch(r"provider-[a-f0-9]{32}", directory.name):
        raise ValueError("Select an exact prior public provider run in data/evaluation")
    snapshots, digests = {}, {}
    for name in FILES:
        snapshots[name], digests[name] = read_public(directory / name, directory)
    manifest = snapshots["manifest.json"]
    revision = manifest.get("git_revision", "")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("run_id") != directory.name
        or manifest.get("git_dirty") is not False
        or manifest.get("state") != "error"
        or not isinstance(revision, str)
        or not re.fullmatch(r"[a-f0-9]{40}", revision)
        or manifest.get("suite_sha256") != public_suite_digest()
        or manifest.get("oracle_sha256") != oracle_digest()
        or manifest.get("configured_model") != model
        or manifest.get("endpoint_sha256") != hashlib.sha256(endpoint.encode()).hexdigest()
        or manifest.get("request_limits") != LIMITS
        or not same_grader_source(repo_root, revision)
    ):
        raise ValueError("Prior code, model, endpoint or rubric binding differs")
    ledger = snapshots["request_ledger.json"]
    attempts = ledger.get("attempts", [])
    if (
        ledger.get("run_id") != directory.name
        or ledger.get("limits") != LIMITS
        or ledger.get("automatic_retries") is not False
        or len(attempts) != 2
        or [
            (row.get("provider"), row.get("case_id"), row.get("state"), row.get("http_status"))
            for row in attempts
        ]
        != [("llm", "planning", "returned", 200), ("llm", "native_tool_call", "returned", 400)]
    ):
        raise ValueError("Follow-up requires the exact stopped two-request prior run")
    approval_file = approval_path(root, manifest["approval_id"])
    approval, digests["approval_ledger"] = read_public(approval_file, approval_file.parent)
    if approval != ledger:
        raise ValueError("Original grant differs from the prior request ledger")
    for row in attempts:
        MeasuredUsage.model_validate(row["usage"])
        request = snapshots["request-llm-" + row["case_id"] + ".json"]
        case = next(item for item in CASES if item.id == row["case_id"])
        if (
            payload_digest(request) != row["request_sha256"]
            or request.get("model") != model
            or request.get("max_tokens") != MAX_OUTPUT_TOKENS
            or request.get("messages")
            != [{"role": "system", "content": SYSTEM}, {"role": "user", "content": case.prompt}]
        ):
            raise ValueError("Prior request does not match the exact public contract")
    records = snapshots["cases.json"]
    if not isinstance(records, list) or [row.get("case_id") for row in records] != [
        "planning",
        "native_tool_call",
    ]:
        raise ValueError("Prior case coverage differs")
    record, response = records[0], snapshots["planning-response.json"]
    checks = grade_structured(CASES[0], parse_answer(response["text"]))
    if (
        record.get("status") != "passed"
        or record.get("kind") != "plan"
        or record.get("checks") != checks
        or not all(checks.values())
        or record.get("input_sha256") != payload_digest(asdict(CASES[0]))
        or record.get("output_sha256") != payload_digest(response)
        or response.get("tool_calls") != []
        or record.get("observed_model") != response.get("model")
        or records[1].get("status") != "error"
    ):
        raise ValueError("Prior planning response cannot be independently verified")
    copied = deepcopy(record)
    copied.update(
        evidence_kind="reused_prior", source_run_id=directory.name, source_git_revision=revision
    )
    return PriorPlanning(directory, copied, snapshots, digests, approval_file)


def followup_claim(root: Path, prior: PriorPlanning) -> Path:
    target = (root / "provider-followup-claims" / (prior.directory.name + ".json")).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Follow-up claim escaped the evaluation root")
    return target


def load_stopped_followup(
    directory: Path,
    *,
    repo_root: Path,
    prior: PriorPlanning,
    model: str | None,
    endpoint: str,
    identifier: str,
) -> dict[str, Any]:
    """Continue independent, UNATTEMPTED contracts only; never retry the lost tool RPC."""
    root = (repo_root / "data/evaluation").resolve()
    directory = directory.resolve()
    if directory.parent != root or not re.fullmatch(r"provider-[a-f0-9]{32}", directory.name):
        raise ValueError("Select an exact stopped public follow-up run")
    manifest, _ = read_public(directory / "manifest.json", directory)
    revision = manifest.get("git_revision", "")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Invalid stopped follow-up source revision")
    ledger, _ = read_public(directory / "request_ledger.json", directory)
    records, _ = read_public(directory / "cases.json", directory)
    request, _ = read_public(directory / "request-llm-native_tool_call.json", directory)
    if (
        manifest.get("run_id") != directory.name
        or manifest.get("state") != "error"
        or manifest.get("git_dirty") is not False
        or manifest.get("approval_id") != identifier
        or manifest.get("suite_sha256") != public_suite_digest()
        or manifest.get("oracle_sha256") != oracle_digest()
        or manifest.get("configured_model") != model
        or manifest.get("endpoint_sha256") != hashlib.sha256(endpoint.encode()).hexdigest()
        or manifest.get("prior_evidence") != prior.binding()
        or not same_grader_source(repo_root, manifest["git_revision"])
        or ledger.get("run_id") != directory.name
        or ledger.get("limits") != {"llm": 9, "tavily": 2}
        or ledger.get("blocked_case_ids") != ["planning"]
        or len(ledger.get("attempts", [])) != 1
        or [row.get("case_id") for row in records] != ["planning", "native_tool_call"]
        or records[0] != prior.record
        or records[1].get("status") != "error"
    ):
        raise ValueError("Stopped follow-up source, grant or case binding differs")
    attempt = ledger["attempts"][0]
    if (
        attempt.get("provider") != "llm"
        or attempt.get("case_id") != "native_tool_call"
        or attempt.get("http_status") != 200
        or attempt.get("state") != "unknown"
        or attempt.get("request_sha256") != payload_digest(request)
        or request.get("model") != model
        or request.get("tool_choice") != "auto"
        or request.get("messages")
        != [{"role": "system", "content": SYSTEM}, {"role": "user", "content": CASES[1].prompt}]
        or request.get("max_tokens") != MAX_OUTPUT_TOKENS
        or records[1].get("input_sha256") != payload_digest(asdict(CASES[1]))
    ):
        raise ValueError("Stopped native request does not match its consumed reservation")
    MeasuredUsage.model_validate(attempt["usage"])
    claim = followup_claim(root, prior)
    binding, _ = read_public(claim, claim.parent)
    if binding.get("followup_run_id") != directory.name or binding.get("approval_id") != identifier:
        raise ValueError("Follow-up ownership claim differs")
    return {"directory": directory, "ledger": ledger, "records": records}
