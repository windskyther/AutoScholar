"""Explicit mock-transport contracts; these are NOT real provider capability results."""

import argparse
import base64
import gzip
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.coding.sandbox import (
    SandboxArtifact,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.evaluation import provider_prior as prior_module
from autoscholar.evaluation import provider_validation as validation
from autoscholar.evaluation.provider_cases import (
    CASES,
    PLAN,
    grade_code,
    grade_research,
    grade_structured,
    parse_answer,
)
from autoscholar.evaluation.provider_transport import (
    ApprovalLedger,
    GuardedTransport,
    approval_path,
    no_secrets,
)

REVISION = "a" * 40
POSITIVE_SOURCE = "def stable_unique(values):\n    return list(dict.fromkeys(values))\n"


def case(identifier: str) -> Any:
    return next(item for item in CASES if item.id == identifier)


def responses() -> dict[str, dict[str, Any]]:
    return {
        "planning": deepcopy(PLAN),
        "native_tool_result": {"result": 472},
        "coding_generation": {"source": POSITIVE_SOURCE},
        "coding_repair": {"source": POSITIVE_SOURCE},
        "reviewer": {
            "status": "REPLAN",
            "issues": [
                {
                    "code": "invalid_accuracy",
                    "step_id": "train",
                    "message": "Recomputed accuracy 0.75 disagrees with 1.0",
                }
            ],
            "suggested_steps": ["train"],
        },
        "replanning": {
            "plan": deepcopy(PLAN),
            "rerun_steps": ["train"],
            "reason": "Preserve approved source and retry invalid experiment only",
        },
        "research_english": {
            "returns_new_list": True,
            "mutates_input": False,
            "citations": [
                {"id": "E1", "quote": "Return a new sorted list from the items in iterable."}
            ],
        },
        "research_chinese": {
            "sorts_in_place": True,
            "return_value": None,
            "citations": [
                {
                    "id": "E1",
                    "quote": "Sort the list in place. Methods that modify lists return None.",
                }
            ],
        },
        "writer": {
            "latest_accuracy": 0.81,
            "sample_count": 128,
            "full_benchmark_claim": False,
            "source_attempt_id": "attempt-2",
            "limitation": "A small CPU subset is not a full scientific benchmark.",
        },
    }


@pytest.mark.parametrize(
    "identifier", ["planning", "native_tool_result", "reviewer", "replanning", "writer"]
)
def test_production_schemas_and_independent_structured_grading(identifier: str) -> None:
    assert all(grade_structured(case(identifier), responses()[identifier]).values())


@pytest.mark.parametrize(
    "identifier", ["planning", "native_tool_result", "reviewer", "replanning", "writer"]
)
def test_plausible_wrong_answer_does_not_pass(identifier: str) -> None:
    answer = responses()[identifier]
    if identifier == "planning":
        answer["steps"][2]["description"] = "Do a full GPU benchmark"
    elif identifier == "native_tool_result":
        answer["result"] = 470
    elif identifier == "reviewer":
        answer.update(status="PASS", issues=[], suggested_steps=[])
    elif identifier == "replanning":
        answer["rerun_steps"] = ["code", "train"]
    else:
        answer["latest_accuracy"] = 0.97
    assert not all(grade_structured(case(identifier), answer).values())


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', "[1,2]", '{"x":NaN}', "```json\n{}\n```"])
def test_ambiguous_or_invalid_json_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_answer(raw)


@pytest.mark.parametrize("identifier", ["research_english", "research_chinese"])
def test_citation_existence_is_not_claim_support(identifier: str) -> None:
    answer = responses()[identifier]
    evidence = [{"id": "E1", "content": answer["citations"][0]["quote"]}]
    assert all(grade_research(case(identifier), answer, evidence).values())
    evidence[0]["content"] = "An unrelated source passage"
    assert not all(grade_research(case(identifier), answer, evidence).values())
    answer["citations"][0]["quote"] = evidence[0]["content"]
    graded = grade_research(case(identifier), answer, evidence)
    assert graded["exact_source_quote_binding"] and not graded["closed_world_support"]


def test_one_shot_grant_limits_survive_recreation(tmp_path: Path) -> None:
    path = approval_path(tmp_path, "public-test-approval")
    ledger = ApprovalLedger(path, run_id="explicit-test")
    for index in range(10):
        ledger.reserve("llm", f"test-{index}", {"public": index})
    with pytest.raises(ValueError, match="allowance"):
        ledger.reserve("llm", "test-over-limit", {})
    ledger.reserve("tavily", "test-search-1", {})
    ledger.reserve("tavily", "test-search-2", {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", "test-search-3", {})
    with pytest.raises(ValueError, match="retries"):
        ledger.reserve("llm", "test-0", {})
    with pytest.raises(FileExistsError):
        ApprovalLedger(path, run_id="another-run-cannot-reset-grant")
    assert len(json.loads(path.read_text())["attempts"]) == 12


@pytest.mark.parametrize("identifier", ["../escape", "C:/private", "secret/key", ""])
def test_approval_identifier_cannot_be_a_path(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(ValueError):
        approval_path(tmp_path, identifier)


@pytest.mark.parametrize("payload", [{"nested": ["private-key"]}, {"private-key": "text"}])
def test_keys_never_enter_public_payloads(payload: object) -> None:
    with pytest.raises(ValueError, match="Credential"):
        no_secrets(payload, ("private-key",))


@pytest.mark.parametrize("fault", ["wrong_endpoint", "secret", "oversize", "repeated", "network"])
async def test_guard_bounds_actual_transport_and_no_retry(tmp_path: Path, fault: str) -> None:
    ledger = ApprovalLedger(tmp_path / "approval.json", run_id="explicit-test")
    sent = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        assert len(ledger.attempts) == 1 and ledger.attempts[0]["state"] == "reserved"
        if fault == "network":
            raise httpx.ConnectError("Do not export a secret-bearing error", request=request)
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
        )

    guard = GuardedTransport(
        provider="llm",
        endpoint="https://test.invalid/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=("private-key",),
        delegate=httpx.MockTransport(handler),
    )
    guard.case_id = "public-test"
    payload = {"messages": [{"role": "user", "content": "public"}]}
    if fault == "secret":
        payload["messages"][0]["content"] = "private-key"
    elif fault == "oversize":
        payload["messages"][0]["content"] = "x" * 40000
    endpoint = (
        "https://wrong.invalid/chat/completions"
        if fault == "wrong_endpoint"
        else str(guard.endpoint)
    )
    async with httpx.AsyncClient(transport=guard, follow_redirects=False) as client:
        if fault == "repeated":
            await client.post(endpoint, json=payload)
        with pytest.raises((ValueError, httpx.ConnectError)):
            await client.post(endpoint, json=payload)
    assert len(sent) == (1 if fault in ("repeated", "network") else 0)
    if fault == "network":
        assert ledger.attempts[0]["state"] == "unknown"
        assert ledger.attempts[0]["usage"]["total_tokens"] is None
    if sent:
        assert sent[0]["max_tokens"] == 4096


async def test_compressed_http_200_is_not_decoded_twice(tmp_path: Path) -> None:
    ledger = ApprovalLedger(tmp_path / "approval.json", run_id="explicit-test")
    payload = {
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        "public_result": "native calculator response",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=gzip.compress(json.dumps(payload).encode()),
        )

    guard = GuardedTransport(
        provider="llm",
        endpoint="https://test.invalid/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        delegate=httpx.MockTransport(handler),
    )
    guard.case_id = "public-compression-contract"
    async with httpx.AsyncClient(transport=guard) as client:
        result = await client.post(str(guard.endpoint), json={"messages": []})
    assert result.json() == payload and "content-encoding" not in result.headers
    assert ledger.attempts[0]["state"] == "returned"
    assert ledger.attempts[0]["usage"]["total_tokens"] == 3


class ExplicitSandboxStub:
    """Fake sandbox contract ONLY; actual code execution is verified separately."""

    context = "local-test"
    container = "autoscholar-eval-test-sandbox-manager-1"

    def __init__(self, passed: int = 8) -> None:
        self.passed = passed
        self.requests: list[SandboxRunRequest] = []

    async def health(self) -> SandboxHealth:
        return SandboxHealth(
            status="ok", engine=True, image=True, mnist_dataset=False, image_sha256="b" * 64
        )

    async def _metadata(self, *args: str) -> bytes:
        return b""

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.requests.append(request)
        artifacts = []
        if request.action == "run_python":
            raw = json.dumps(
                {"schema_version": 1, "passed_count": self.passed, "total_count": 8}
            ).encode()
            artifacts = [
                SandboxArtifact(
                    path="outputs/eval_oracle.json",
                    size_bytes=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    data_base64=base64.b64encode(raw).decode(),
                )
            ]
        return SandboxRunResult(
            status="succeeded",
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
            artifacts=artifacts,
        )

    async def close(self) -> None:
        pass


async def test_code_has_independent_oracle_and_no_host_execution() -> None:
    sandbox = ExplicitSandboxStub()
    assert all((await grade_code({"source": POSITIVE_SOURCE}, sandbox)).values())
    assert sandbox.requests[0].action == "static_check"
    assert "oracle_cases.json" not in sandbox.requests[0].files
    assert "oracle_cases.json" in sandbox.requests[1].files
    rejected = ExplicitSandboxStub(passed=7)
    assert not all((await grade_code({"source": POSITIVE_SOURCE}, rejected)).values())
    with pytest.raises(ValueError):
        await grade_code(
            {"source": "import os\ndef stable_unique(values):\n    return values\n"}, sandbox
        )


@pytest.mark.parametrize(
    "fault", [None, "wrong_writer", "no_usage", "rate_limit", "unsupported_parameter", "bad_native"]
)
async def test_full_cli_contract_with_explicit_mock_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
    prior_run: Path | None = None,
    continue_from: Path | None = None,
) -> None:
    fake = responses()
    if fault == "wrong_writer":
        fake["writer"]["latest_accuracy"] = 0.97
    for key in ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        validation,
        "dotenv_values",
        lambda *args, **kwargs: {
            "LLM_API_KEY": "llm-fixture-key-123",
            "LLM_MODEL": "explicit-test-stub",
            "LLM_BASE_URL": "https://test.invalid",
            "TAVILY_API_KEY": "search-fixture-key-123",
        },
    )
    monkeypatch.setattr(validation, "_git_state", lambda _: (REVISION, False))
    monkeypatch.setattr(validation, "DockerEvaluationSandbox", lambda _: ExplicitSandboxStub())
    calls = []

    def factory(**kwargs: Any) -> GuardedTransport:
        transport: GuardedTransport

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append((transport.provider, transport.case_id))
            if fault in ("rate_limit", "unsupported_parameter"):
                return httpx.Response(
                    429 if fault == "rate_limit" else 400,
                    json={"error": {"message": "Do not print this body"}},
                )
            if transport.provider == "tavily":
                selected = (
                    "research_chinese"
                    if "None" in json.loads(request.content)["query"]
                    else "research_english"
                )
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "title": "Public Python documentation",
                                "url": "https://docs.python.org/3/library/stdtypes.html",
                                "content": fake[selected]["citations"][0]["quote"],
                            }
                        ]
                    },
                )
            identifier = transport.case_id
            if identifier == "native_tool_call":
                assert json.loads(request.content)["tool_choice"] == "auto"
            message: dict[str, Any] = {
                "role": "assistant",
                "content": json.dumps(fake[identifier]) if identifier in fake else None,
            }
            if identifier == "native_tool_call":
                message["tool_calls"] = [
                    {
                        "id": "public-call-1",
                        "type": "function",
                        "function": {
                            "name": "wrong" if fault == "bad_native" else "calculator",
                            "arguments": '{"expression":"(17*23)+(9**2)"}',
                        },
                    }
                ]
            response: dict[str, Any] = {
                "id": "explicit-stub",
                "object": "chat.completion",
                "created": 1,
                "model": "explicit-test-stub",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            }
            if fault != "no_usage":
                response["usage"] = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            return httpx.Response(200, json=response)

        transport = GuardedTransport(**kwargs, delegate=httpx.MockTransport(handler))
        return transport

    monkeypatch.setattr(validation, "GuardedTransport", factory)
    args = argparse.Namespace(
        allow_paid=True,
        approval_id="explicit-test-approval",
        sandbox_container="explicit-test-controller",
        prior_run=prior_run,
        continue_unattempted=continue_from,
    )
    directory, accepted = await validation.run_providers(args, repo_root=tmp_path)
    summary = json.loads((directory / "provider_acceptance.json").read_text())
    assert accepted is (fault in (None, "no_usage") and continue_from is None)
    assert len(calls) <= 12
    assert sum(provider == "llm" for provider, _ in calls) <= 10
    assert sum(provider == "tavily" for provider, _ in calls) <= 2
    assert summary["phase10_overall_acceptance"] is False
    if fault == "no_usage":
        assert summary["vendor_tokens"] is None and summary["known_vendor_tokens"] == 0
    elif fault in ("rate_limit", "unsupported_parameter"):
        assert len(calls) == summary["recorded"] == 1 and summary["planned_llm_cases"] == 10
        assert summary["state"] == "error" and summary["cases"][0]["error_type"]
    elif fault == "bad_native":
        assert len(calls) == 11 and summary["cases"][2]["status"] == "skipped"
    elif continue_from is not None:
        assert len(calls) == 9 and summary["vendor_tokens"] == 24
        assert summary["physical_requests"] == {"llm": 8, "tavily": 2}
        assert summary["cumulative_requests_total"] == 12
        assert summary["new_requests_in_continuation"] == 9
        assert summary["passed"] == 8 and summary["cases"][2]["status"] == "skipped"
        assert all(
            identifier not in ("planning", "native_tool_call", "native_tool_result")
            for _, identifier in calls
        )
    elif prior_run is not None:
        assert len(calls) == 11 and summary["vendor_tokens"] == 27
        assert summary["request_limits"] == {"llm": 9, "tavily": 2}
        assert summary["reused_planning_cases"] == 1
        assert summary["cumulative_requests_total"] == 13
        assert summary["cumulative_vendor_tokens"] is None
        assert summary["cumulative_known_vendor_tokens"] == 30
        assert summary["cases"][0]["evidence_kind"] == "reused_prior"
        assert all(identifier != "planning" for _, identifier in calls)
        assert not (directory / "request-llm-planning.json").exists()
    else:
        assert len(calls) == 12 and summary["vendor_tokens"] == 30
    with pytest.raises(ValueError, match="already"):
        await validation.run_providers(args, repo_root=tmp_path)
    assert "llm-fixture-key-123" not in (directory / "evaluation_report.md").read_text()


def make_prior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Construct explicit synthetic primary evidence, not a live-provider result."""
    from dataclasses import asdict

    from autoscholar.evaluation.datasets import payload_digest
    from autoscholar.evaluation.models import MeasuredUsage
    from autoscholar.evaluation.provider_cases import SYSTEM, oracle_digest, public_suite_digest

    root = tmp_path / "data/evaluation"
    directory = root / ("provider-" + "0" * 32)
    directory.mkdir(parents=True)
    monkeypatch.setattr(prior_module, "same_grader_source", lambda *args: True)
    ledger = ApprovalLedger(approval_path(root, "prior-stub-approval"), run_id=directory.name)
    payloads: dict[str, Any] = {}
    for identifier, status in (("planning", 200), ("native_tool_call", 400)):
        request = {
            "model": "explicit-test-stub",
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": case(identifier).prompt},
            ],
        }
        payloads["request-llm-" + identifier + ".json"] = request
        index = ledger.reserve("llm", identifier, request)
        ledger.finish(
            index,
            state="returned",
            status=status,
            usage=MeasuredUsage(
                model_calls=1,
                external_api_calls=1,
                input_tokens=2 if status == 200 else None,
                output_tokens=1 if status == 200 else None,
                total_tokens=3 if status == 200 else None,
            ),
        )
    response = {
        "text": json.dumps(responses()["planning"]),
        "model": "explicit-test-stub",
        "tool_calls": [],
    }
    record = {
        "case_id": "planning",
        "kind": "plan",
        "status": "passed",
        "checks": grade_structured(case("planning"), responses()["planning"]),
        "input_sha256": payload_digest(asdict(case("planning"))),
        "output_sha256": payload_digest(response),
        "observed_model": "explicit-test-stub",
    }
    payloads.update(
        {
            "planning-response.json": response,
            "cases.json": [record, {"case_id": "native_tool_call", "status": "error"}],
            "request_ledger.json": ledger.data,
            "manifest.json": {
                "schema_version": 1,
                "run_id": directory.name,
                "git_revision": REVISION,
                "git_dirty": False,
                "state": "error",
                "suite_sha256": public_suite_digest(),
                "oracle_sha256": oracle_digest(),
                "configured_model": "explicit-test-stub",
                "endpoint_sha256": hashlib.sha256(
                    b"https://test.invalid/chat/completions"
                ).hexdigest(),
                "request_limits": {"llm": 10, "tavily": 2},
                "approval_id": "prior-stub-approval",
            },
        }
    )
    for filename, payload in payloads.items():
        (directory / filename).write_text(json.dumps(payload), encoding="utf-8")
    return directory


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "request",
        "response",
        "source",
        "model",
        "endpoint",
        "checks",
        "extra_attempt",
        "grant",
        "case_order",
        "running",
    ],
)
def test_prior_planning_regraded_from_bound_primary_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None
) -> None:
    directory = make_prior(tmp_path, monkeypatch)
    selected = "manifest.json"
    if mutation == "source":
        monkeypatch.setattr(prior_module, "same_grader_source", lambda *args: False)
    elif mutation is not None:
        selected = {
            "request": "request-llm-planning.json",
            "response": "planning-response.json",
            "checks": "cases.json",
            "extra_attempt": "request_ledger.json",
            "case_order": "cases.json",
        }.get(mutation, "manifest.json")
        path = directory / selected
        payload = json.loads(path.read_text())
        if mutation == "request":
            payload["messages"][1]["content"] = "Changed public prompt"
        elif mutation == "response":
            payload["text"] = "{}"
        elif mutation == "model":
            payload["configured_model"] = "different-model"
        elif mutation == "endpoint":
            payload["endpoint_sha256"] = "c" * 64
        elif mutation == "checks":
            payload[0]["checks"] = {"fake": True}
        elif mutation == "extra_attempt":
            payload["attempts"].append(payload["attempts"][0])
        elif mutation == "grant":
            path = approval_path(tmp_path / "data/evaluation", "prior-stub-approval")
            payload = {"attempts": []}
        elif mutation == "case_order":
            payload.reverse()
        elif mutation == "running":
            payload["state"] = "running"
        path.write_text(json.dumps(payload))
    kwargs = {
        "repo_root": tmp_path,
        "model": "explicit-test-stub",
        "endpoint": "https://test.invalid/chat/completions",
    }
    if mutation:
        with pytest.raises(ValueError):
            prior_module.load_prior(directory, **kwargs)  # type: ignore[arg-type]
    else:
        prior = prior_module.load_prior(directory, **kwargs)  # type: ignore[arg-type]
        assert prior.unchanged() and prior.record["evidence_kind"] == "reused_prior"
        (directory / "planning-response.json").write_text("{}")
        assert not prior.unchanged()


async def test_manual_followup_reuses_planning_without_resetting_prior_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = make_prior(tmp_path, monkeypatch)
    before = {name: (prior / name).read_bytes() for name in prior_module.FILES}
    await test_full_cli_contract_with_explicit_mock_transport(
        tmp_path, monkeypatch, None, prior_run=prior
    )
    assert before == {name: (prior / name).read_bytes() for name in prior_module.FILES}
    # A new grant ID cannot repeat the same follow-up and reset the cumulative ceiling.
    with pytest.raises(ValueError, match="already has a follow-up"):
        await validation.run_providers(
            argparse.Namespace(
                allow_paid=True,
                approval_id="another-fresh-grant",
                prior_run=prior,
                sandbox_container="explicit-test-controller",
            ),
            repo_root=tmp_path,
        )


def test_followup_ledger_hard_limits_and_blocked_planning(tmp_path: Path) -> None:
    ledger = ApprovalLedger(
        tmp_path / "followup.json",
        run_id="explicit-test",
        limits={"llm": 9, "tavily": 2},
        blocked_case_ids=("planning",),
    )
    with pytest.raises(ValueError, match="must not be dispatched"):
        ledger.reserve("llm", "planning", {})
    for index in range(9):
        ledger.reserve("llm", f"public-{index}", {})
    with pytest.raises(ValueError, match="allowance"):
        ledger.reserve("llm", "public-tenth", {})
    assert ledger.limits == {"llm": 9, "tavily": 2}
    with pytest.raises(ValueError, match="ceiling"):
        ApprovalLedger(tmp_path / "invalid.json", run_id="invalid", limits={"llm": 11, "tavily": 2})


async def test_no_explicit_approval_never_loads_private_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No .env reading without explicit approval")

    monkeypatch.setattr(validation, "load_explicit_settings", forbidden)
    with pytest.raises(ValueError, match="explicit"):
        await validation.run_providers(argparse.Namespace(allow_paid=False), repo_root=tmp_path)


async def test_continue_only_unattempted_independent_contracts_preserves_consumed_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import asdict

    from autoscholar.evaluation.datasets import payload_digest
    from autoscholar.evaluation.models import MeasuredUsage
    from autoscholar.evaluation.provider_cases import SYSTEM, oracle_digest, public_suite_digest

    first = make_prior(tmp_path, monkeypatch)
    prior = prior_module.load_prior(
        first,
        repo_root=tmp_path,
        model="explicit-test-stub",
        endpoint="https://test.invalid/chat/completions",
    )
    root = tmp_path / "data/evaluation"
    stopped = root / ("provider-" + "1" * 32)
    stopped.mkdir()
    identifier = "explicit-test-approval"
    ledger = ApprovalLedger(
        approval_path(root, identifier),
        run_id=stopped.name,
        limits={"llm": 9, "tavily": 2},
        blocked_case_ids=("planning",),
    )
    request = {
        "model": "explicit-test-stub",
        "max_tokens": 4096,
        "tool_choice": "auto",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": case("native_tool_call").prompt},
        ],
    }
    index = ledger.reserve("llm", "native_tool_call", request)
    ledger.finish(
        index,
        state="unknown",
        status=200,
        usage=MeasuredUsage(
            model_calls=1, external_api_calls=1, input_tokens=2, output_tokens=1, total_tokens=3
        ),
    )
    records = [
        prior.record,
        {
            "case_id": "native_tool_call",
            "kind": "tool_call",
            "status": "error",
            "checks": {},
            "input_sha256": payload_digest(asdict(case("native_tool_call"))),
        },
    ]
    manifest = {
        "run_id": stopped.name,
        "state": "error",
        "git_dirty": False,
        "approval_id": identifier,
        "git_revision": REVISION,
        "suite_sha256": public_suite_digest(),
        "oracle_sha256": oracle_digest(),
        "configured_model": "explicit-test-stub",
        "endpoint_sha256": hashlib.sha256(b"https://test.invalid/chat/completions").hexdigest(),
        "prior_evidence": prior.binding(),
    }
    payloads = {
        "manifest.json": manifest,
        "cases.json": records,
        "request_ledger.json": ledger.data,
        "request-llm-native_tool_call.json": request,
    }
    for name, payload in payloads.items():
        (stopped / name).write_text(json.dumps(payload))
    claim = prior_module.followup_claim(root, prior)
    claim.parent.mkdir()
    claim.write_text(json.dumps({"followup_run_id": stopped.name, "approval_id": identifier}))
    before = {name: (stopped / name).read_bytes() for name in payloads}
    await test_full_cli_contract_with_explicit_mock_transport(
        tmp_path, monkeypatch, None, prior_run=first, continue_from=stopped
    )
    assert before == {name: (stopped / name).read_bytes() for name in payloads}
    retained = json.loads(approval_path(root, identifier).read_text())["attempts"]
    assert len(retained) == 10 and retained[0]["state"] == "unknown"
