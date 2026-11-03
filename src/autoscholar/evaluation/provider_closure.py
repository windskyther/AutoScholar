"""Offline primary-evidence audit for bounded provider closure; never load .env."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscholar.agent.tools.calculator import CalculatorTool
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import MeasuredUsage
from autoscholar.evaluation.provider_cases import (
    CASE_IDS,
    CASES,
    SYSTEM,
    grade_structured,
    parse_answer,
)
from autoscholar.evaluation.provider_confirmation import (
    ToolConfirmationEvidence,
    load_tool_confirmation,
)
from autoscholar.evaluation.provider_prior import load_prior, read_public, same_grader_source
from autoscholar.evaluation.provider_transport import approval_path


@dataclass
class ProviderClosureEvidence:
    previous: ToolConfirmationEvidence
    tools_directory: Path
    tools: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    digests: dict[Path, str]
    evidence: list[dict[str, str]]
    model: str
    endpoint_sha256: str

    def unchanged(self) -> bool:
        try:
            return self.previous.unchanged() and all(
                read_public(path, path.parent)[1] == digest for path, digest in self.digests.items()
            )
        except (OSError, ValueError):
            return False

    def binding(self) -> dict[str, Any]:
        root = self.tools_directory.parent
        return {
            "independent_run": self.previous.directory.name,
            "tool_run": self.tools_directory.name,
            "original_run": self.previous.prior.directory.name,
            "previous_binding": self.previous.binding(),
            "file_sha256": {
                path.relative_to(root).as_posix(): sha for path, sha in self.digests.items()
            },
            "configured_model": self.model,
            "endpoint_sha256": self.endpoint_sha256,
        }


async def load_provider_closure(
    *, repo_root: Path, original_run: Path, independent_run: Path, tool_run: Path
) -> ProviderClosureEvidence:
    root = (repo_root / "data/evaluation").resolve()
    directory = tool_run.resolve()
    if directory.parent != root or not re.fullmatch(r"provider-[a-f0-9]{32}", directory.name):
        raise ValueError("Select the exact public tool-confirmation run")
    snapshots, digests = {}, {}

    def capture(path: Path) -> Any:
        value, digests[path] = read_public(path, root)
        return value

    for name in ("manifest.json", "cases.json", "request_ledger.json"):
        snapshots[name] = capture(directory / name)
    manifest = snapshots["manifest.json"]
    model, fingerprint = manifest["configured_model"], manifest["endpoint_sha256"]
    prior = load_prior(
        original_run, repo_root=repo_root, model=model, expected_endpoint_sha256=fingerprint
    )
    previous = load_tool_confirmation(
        independent_run,
        repo_root=repo_root,
        prior=prior,
        model=model,
        expected_endpoint_sha256=fingerprint,
    )
    records, ledger = snapshots["cases.json"], snapshots["request_ledger.json"]
    blocked = [identifier for identifier in CASE_IDS if identifier not in CASE_IDS[1:3]]
    if (
        manifest.get("run_id") != directory.name
        or manifest.get("state") != "completed"
        or manifest.get("git_dirty") is not False
        or not same_grader_source(repo_root, manifest.get("git_revision", ""))
        or manifest.get("tool_confirmation_of") != previous.binding()
        or manifest.get("prior_evidence") != prior.binding()
        or manifest.get("suite_sha256") != prior.snapshots["manifest.json"]["suite_sha256"]
        or manifest.get("oracle_sha256") != prior.snapshots["manifest.json"]["oracle_sha256"]
        or manifest.get("request_limits") != {"llm": 2, "tavily": 0}
        or ledger.get("run_id") != directory.name
        or ledger.get("limits") != {"llm": 2, "tavily": 0}
        or ledger.get("blocked_case_ids") != blocked
        or ledger.get("automatic_retries") is not False
        or [row.get("case_id") for row in records] != list(CASE_IDS[1:3])
        or any(row.get("status") != "passed" for row in records)
        or [row.get("case_id") for row in ledger.get("attempts", [])] != list(CASE_IDS[1:3])
    ):
        raise ValueError("Tool confirmation source, rubric, allowance or coverage differs")
    grant = capture(approval_path(root, manifest["approval_id"]))
    claim = capture(
        root / "provider-tool-confirmation-claims" / (previous.directory.name + ".json")
    )
    if grant != ledger or claim != {
        "from_run": previous.directory.name,
        "run_id": directory.name,
        "approval_id": manifest["approval_id"],
    }:
        raise ValueError("Tool confirmation grant/ownership differs")
    requests, outputs, raw_outputs = [], [], []
    for row, record in zip(ledger["attempts"], records, strict=True):
        if (row.get("provider"), row.get("state"), row.get("http_status")) != (
            "llm",
            "returned",
            200,
        ):
            raise ValueError("Tool request is not a known returned LLM response")
        MeasuredUsage.model_validate(row["usage"])
        request = capture(directory / ("request-llm-" + row["case_id"] + ".json"))
        output = capture(directory / (row["case_id"] + "-response.json"))
        raw_output = capture(directory / ("response-llm-" + row["case_id"] + ".json"))
        if (
            payload_digest(request) != row["request_sha256"]
            or payload_digest(output) != record["output_sha256"]
        ):
            raise ValueError("Tool request/response hash differs")
        requests.append(request)
        outputs.append(output)
        raw_outputs.append(raw_output)
        if request.get("model") != model or request.get("max_tokens") != 4096:
            raise ValueError("Tool model or response cap differs")
    calls = outputs[0]["tool_calls"]
    if len(calls) != 1 or calls[0]["name"] != "calculator":
        raise ValueError("Missing actual native Calculator call")
    computed = await CalculatorTool().execute(calls[0]["arguments"])
    messages = requests[1]["messages"]
    native = raw_outputs[0]["choices"][0]["message"]
    native_calls = native.get("tool_calls", [])
    forwarded_calls = messages[2].get("tool_calls", [])
    checks = grade_structured(CASES[2], parse_answer(outputs[1]["text"]))
    if (
        not computed.succeeded
        or computed.output != "472"
        or not all(checks.values())
        or records[1].get("checks") != checks
        or requests[0].get("messages")
        != [{"role": "system", "content": SYSTEM}, {"role": "user", "content": CASES[1].prompt}]
        or requests[0].get("tool_choice") != "auto"
        or len(messages) != 5
        or messages[0] != {"role": "system", "content": SYSTEM}
        or messages[1].get("content") != CASES[1].prompt
        or messages[3]
        != {"role": "tool", "tool_call_id": calls[0]["id"], "content": computed.output}
        or messages[4].get("content") != CASES[2].prompt
        or len(forwarded_calls) != 1
        or forwarded_calls[0].get("id") != calls[0]["id"]
        or len(native_calls) != 1
        or native_calls[0].get("id") != calls[0]["id"]
        or native_calls[0]["function"].get("name") != calls[0]["name"]
        or forwarded_calls[0]["function"].get("name") != calls[0]["name"]
        or json.loads(native_calls[0]["function"]["arguments"]) != calls[0]["arguments"]
        or messages[2].get("reasoning_content") != native.get("reasoning_content")
        or json.loads(forwarded_calls[0]["function"]["arguments"]) != calls[0]["arguments"]
        or raw_outputs[1]["choices"][0]["message"]["content"] != outputs[1]["text"]
    ):
        raise ValueError("Tool round-trip cannot be independently verified")
    evidence = capture(previous.directory / "research_chinese-evidence.json")["sources"]
    if (
        not evidence
        or len(evidence) > 3
        or any(source.get("url", "").split("/")[2:3] != ["docs.python.org"] for source in evidence)
    ):
        raise ValueError("Missing bounded authoritative cached evidence")
    return ProviderClosureEvidence(
        previous,
        directory,
        records,
        previous.attempts + ledger["attempts"],
        digests,
        evidence,
        model,
        fingerprint,
    )
