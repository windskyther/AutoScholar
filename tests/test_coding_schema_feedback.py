"""Metered SDK schema rejections get one Snapshot correction before any execution."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.coding.agent import CodingRunError
from autoscholar.core.budget import Budget, BudgetExceeded, BudgetLimits, current_budget
from autoscholar.core.journal import current_journal
from autoscholar.llm.errors import (
    LLMResponseError,
    LLMUpstreamError,
    correctable_tool_schema_error,
)
from autoscholar.llm.models import TokenUsage
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_coding_agent import FakeSandbox, run_result
from tests.test_coding_convergence import setup_agent
from tests.test_llm_provider import configured_settings


def ready(summary: str = "Ready") -> tuple[str, dict[str, Any]]:
    return "submit_code_ready", {"summary": summary}


def completion(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "id": "offline-only",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (name, args) in enumerate(calls)
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


@pytest.mark.parametrize(
    "invalid",
    [
        [ready("x" * 336)],
        [("edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "x" * 1025})],
        [("create_file", {"path": "new.py", "content": "x" * 2049})],
        [ready(123)],  # type: ignore[arg-type]
        [("submit_code_ready", {})],
        [("submit_code_ready", {"summary": "Ready", "extra": True})],
        [
            ("edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "value = 9"}),
            ready("x" * 336),
        ],
    ],
)
async def test_sdk_schema_rejection_corrects_without_executing_any_rejected_batch(
    tmp_path: Path, invalid: list[tuple[str, dict[str, Any]]]
) -> None:
    requests: list[dict[str, Any]] = []
    journal: list[tuple[str, str, bool]] = []

    async def hook(operation: str, kind: str, started: bool) -> None:
        journal.append((operation, kind, started))

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        if len(requests) == 2:
            context = json.loads(payload["messages"][-1]["content"])
            assert "no call was executed" in context["protocol_feedback"]
            assert context["source_files"] == {"app.py": "value = 1\n"}
            assert context["output_contract"]["field_character_limits"]["submit_code_ready"] == {
                "summary": 240
            }
            assert not sandbox.requests and not workspace.source_snapshot("convergence").get(
                "new.py"
            )
        return httpx.Response(200, json=completion(invalid if len(requests) == 1 else [ready()]))

    config = configured_settings()
    config.llm_compatibility_profile = "deepseek"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(config, http_client=client)
        sandbox = FakeSandbox([run_result(), run_result()])
        agent, store, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
        budget = Budget(BudgetLimits(model_calls=12))
        token = current_budget.set(budget)
        journal_token = current_journal.set(hook)
        try:
            result = await agent.run(
                task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True
            )
            assert result.model_calls == len(requests) == budget.used["model_calls"] == 2
            assert result.total_tokens == budget.used["total_tokens"] == 6
            assert result.input_tokens == 4 and result.output_tokens == 2
            assert result.files_written == 0
            assert workspace.source_snapshot("convergence") == {"app.py": "value = 1\n"}
            assert [r.action for r in sandbox.requests] == ["static_check", "run_pytest"]
            traces = (await store.get_task("convergence")).tool_calls
            assert [t.tool_name for t in traces] == ["static_check", "run_pytest"]
            starts = {op for op, kind, started in journal if kind == "llm" and started}
            ends = {op for op, kind, started in journal if kind == "llm" and not started}
            assert len(starts) == 2 and starts == ends
        finally:
            current_budget.reset(token)
            current_journal.reset(journal_token)
            await engine.dispose()


@pytest.mark.parametrize("between", ["none", "valid_edit", "utf8_violation"])
async def test_one_correction_is_shared_and_cannot_be_reset_by_an_accepted_edit(
    tmp_path: Path, between: str
) -> None:
    script = [[ready("x" * 336)], [ready("x" * 336)]]
    if between == "valid_edit":
        script.insert(
            1, [("edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"})]
        )
    if between == "utf8_violation":
        script[0] = [
            ("edit_file", {"path": "app.py", "old_text": "中" * 1024, "new_text": "文" * 1024})
        ]
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert requests <= len(script), "No third correction or budget reset"
        return httpx.Response(200, json=completion(script[requests - 1]))

    config = configured_settings()
    config.llm_compatibility_profile = "deepseek"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sandbox = FakeSandbox([])
        agent, _, engine, workspace = await setup_agent(
            tmp_path, OpenAICompatibleProvider(config, http_client=client), sandbox
        )
        budget = Budget(BudgetLimits())
        token = current_budget.set(budget)
        try:
            with pytest.raises(CodingRunError, match="bounded native"):
                await agent.run(
                    task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True
                )
            assert requests == budget.used["model_calls"] == len(script)
            assert budget.used["total_tokens"] == 3 * requests
            assert not sandbox.requests
            assert workspace.source_snapshot("convergence") == {
                "app.py": "value = 2\n" if between == "valid_edit" else "value = 1\n"
            }
        finally:
            current_budget.reset(token)
            await engine.dispose()


@pytest.mark.parametrize(
    "stop",
    [
        "length",
        "malformed",
        "missing_usage",
        "bad_usage",
        "http503",
        "non_snapshot",
        "tokens",
        "reserve",
        "calls",
        "static_failure",
    ],
)
async def test_feedback_never_bypasses_terminal_errors_budgets_or_validation(
    tmp_path: Path, stop: str
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if stop == "http503":
            return httpx.Response(503, json={"error": {"message": "offline overload"}})
        payload = completion(
            [ready("x" * (1001 if stop == "non_snapshot" else 336))] if requests == 1 else [ready()]
        )
        if stop == "length":
            payload["choices"][0]["finish_reason"] = "length"
        elif stop == "malformed":
            payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
                "{} trailing"
            )
        elif stop == "missing_usage":
            payload.pop("usage")
        elif stop == "bad_usage":
            payload["usage"]["total_tokens"] = 99
        return httpx.Response(200, json=payload)

    config = configured_settings()
    config.llm_compatibility_profile = "deepseek"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sandbox = FakeSandbox([run_result("failed", exit_code=1)])
        agent, _, engine, workspace = await setup_agent(
            tmp_path, OpenAICompatibleProvider(config, http_client=client), sandbox
        )
        budget = Budget(
            BudgetLimits(
                model_calls=1 if stop == "calls" else 2 if stop == "reserve" else 12,
                total_tokens=3 if stop == "tokens" else 120000,
            )
        )
        token = current_budget.set(budget)
        try:
            with pytest.raises((LLMUpstreamError, BudgetExceeded, CodingRunError)):
                await agent.run(
                    task_id="convergence",
                    objective="Inspect",
                    plan=[],
                    snapshot_mode=stop != "non_snapshot",
                    model_call_reserve=1 if stop == "reserve" else 0,
                )
            assert requests == budget.used["model_calls"] == (2 if stop == "static_failure" else 1)
            assert [r.action for r in sandbox.requests] == (
                ["static_check"] if stop == "static_failure" else []
            )
            assert workspace.source_snapshot("convergence") == {"app.py": "value = 1\n"}
            if stop not in {"http503", "missing_usage", "bad_usage"}:
                assert budget.used["total_tokens"] == 3 * requests
        finally:
            current_budget.reset(token)
            await engine.dispose()


@pytest.mark.parametrize(
    "change",
    [
        {"usage": None},
        {"retryable": False},
        {"finish_reason": "length"},
        {"finish_reason": None},
        {"validation_reason": "extra_data"},
        {"code": "llm_empty_response"},
        {"model": None},
        {"usage": TokenUsage(2, 1, 4)},
        {"usage": TokenUsage(-1, 2, 1)},
    ],
)
def test_only_complete_known_schema_rejections_are_correctable(change: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "code": "llm_invalid_tool_call",
        "message": "untrusted text is not feedback",
        "usage": TokenUsage(2, 1, 3),
        "model": "test-model",
        "finish_reason": "tool_calls",
        "validation_reason": "tool_schema_violation",
    }
    assert correctable_tool_schema_error(LLMResponseError(**values))
    assert not correctable_tool_schema_error(LLMResponseError(**(values | change)))
