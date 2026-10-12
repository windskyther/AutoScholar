"""Explicit mock-transport contracts; these are NOT real provider capability results."""

import argparse
import base64
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


@pytest.mark.parametrize("fault", [None, "wrong_writer", "no_usage", "rate_limit", "bad_native"])
async def test_full_cli_contract_with_explicit_mock_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
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
            if fault == "rate_limit":
                return httpx.Response(429, json={"error": {"message": "Do not print this body"}})
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
    )
    if fault == "rate_limit":
        with pytest.raises(ValueError, match="Real verification stopped"):
            await validation.run_providers(args, repo_root=tmp_path)
        directory = next((tmp_path / "data/evaluation").glob("provider-*"))
        # The approval directory is also provider-*, select the actual report below.
        directory = next(
            path.parent for path in (tmp_path / "data/evaluation").rglob("provider_acceptance.json")
        )
        accepted = False
    else:
        directory, accepted = await validation.run_providers(args, repo_root=tmp_path)
    summary = json.loads((directory / "provider_acceptance.json").read_text())
    assert accepted is (fault in (None, "no_usage"))
    assert len(calls) <= 12
    assert sum(provider == "llm" for provider, _ in calls) <= 10
    assert sum(provider == "tavily" for provider, _ in calls) <= 2
    assert summary["phase10_overall_acceptance"] is False
    if fault == "no_usage":
        assert summary["vendor_tokens"] is None and summary["known_vendor_tokens"] == 0
    elif fault == "rate_limit":
        assert len(calls) == summary["recorded"] == 1 and summary["planned_llm_cases"] == 10
    elif fault == "bad_native":
        assert len(calls) == 11 and summary["cases"][2]["status"] == "skipped"
    else:
        assert len(calls) == 12 and summary["vendor_tokens"] == 30
    with pytest.raises(ValueError, match="Approval already exists"):
        await validation.run_providers(args, repo_root=tmp_path)
    assert "llm-fixture-key-123" not in (directory / "evaluation_report.md").read_text()


async def test_no_explicit_approval_never_loads_private_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("No .env reading without explicit approval")

    monkeypatch.setattr(validation, "load_explicit_settings", forbidden)
    with pytest.raises(ValueError, match="explicit"):
        await validation.run_providers(argparse.Namespace(allow_paid=False), repo_root=tmp_path)
