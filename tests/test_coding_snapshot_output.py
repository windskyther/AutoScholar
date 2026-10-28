"""Small snapshot edits and atomic refusal of truncated native responses, no vendor calls."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.coding.agent import CodingRunError
from autoscholar.coding.snapshot import MAX_ARGUMENT_BYTES, snapshot_tool, snapshot_violation
from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.llm.errors import LLMRequestRejected, LLMResponseError
from autoscholar.llm.models import LLMResult, TokenUsage, ToolCall
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_coding_agent import FakeSandbox, run_result
from tests.test_coding_convergence import setup_agent
from tests.test_llm_provider import configured_settings


@pytest.mark.parametrize("second_invalid", [False, True])
async def test_truncated_native_batch_never_executes_even_a_complete_first_edit(
    tmp_path: Path, second_invalid: bool
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        calls = [
            {
                "id": "first",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": json.dumps(
                        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 999"}
                    ),
                },
            }
        ]
        if second_invalid:
            calls.append(
                {
                    "id": "second",
                    "type": "function",
                    "function": {"name": "edit_file", "arguments": '{"path":"app.py","old_text":'},
                }
            )
        return httpx.Response(
            200,
            json={
                "id": "offline-cut",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": None, "tool_calls": calls},
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 8192, "total_tokens": 8194},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(configured_settings(), http_client=client)
        sandbox = FakeSandbox([])
        agent, store, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
        budget = Budget(BudgetLimits(model_calls=12, total_tokens=120000))
        token = current_budget.set(budget)
        try:
            before = workspace.source_snapshot("convergence")
            with pytest.raises(LLMResponseError) as caught:
                await agent.run(
                    task_id="convergence",
                    objective="Inspect existing code",
                    plan=["untrusted extra plan " * 1000],
                    snapshot_mode=True,
                )
            assert caught.value.code == "llm_response_truncated"
            assert caught.value.finish_reason == "length" and not caught.value.retryable
            assert caught.value.usage == TokenUsage(2, 8192, 8194)
            assert budget.used == {
                "model_calls": 1,
                "input_tokens": 2,
                "output_tokens": 8192,
                "total_tokens": 8194,
            }
            assert len(requests) == 1 and requests[0]["tool_choice"] == "required"
            assert requests[0]["parallel_tool_calls"] is False
            assert "untrusted extra plan" not in requests[0]["messages"][0]["content"]
            payload = json.loads(requests[0]["messages"][-1]["content"])
            assert payload["source_files"] == before
            assert payload["objective"] == "Inspect existing code"
            assert workspace.source_snapshot("convergence") == before
            assert not sandbox.requests and not (await store.get_task("convergence")).tool_calls
        finally:
            current_budget.reset(token)
            await engine.dispose()


@pytest.mark.parametrize("bad", ["long_edit", "long_create", "utf8", "extra", "empty", "type"])
async def test_invalid_snapshot_operation_gets_one_correction_without_any_write(
    tmp_path: Path, bad: str
) -> None:
    argument_sets: dict[str, dict[str, Any]] = {
        "long_edit": {"path": "app.py", "old_text": "value = 1", "new_text": "x" * 1025},
        "long_create": {"path": "new.py", "content": "x" * 2049},
        "utf8": {"path": "app.py", "old_text": "中" * 1024, "new_text": "文" * 1024},
        "extra": {
            "path": "app.py",
            "old_text": "value = 1",
            "new_text": "value = 2",
            "overwrite": True,
        },
        "empty": {"path": "app.py", "old_text": "", "new_text": "value = 2"},
        "type": {"path": "app.py", "old_text": 1, "new_text": "value = 2"},
    }
    arguments = argument_sets[bad]

    class InvalidProvider:
        configured = True
        calls = 0

        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            self.calls += 1
            payload = json.loads(messages[-1].content)
            if self.calls == 2:
                assert "no arguments were truncated" in payload["protocol_feedback"]
                assert payload["source_files"] == {"app.py": "value = 1\n"}
            return LLMResult(
                text="",
                model="script",
                usage=TokenUsage(2, 1, 3),
                tool_calls=(
                    ToolCall(
                        id=str(self.calls),
                        name="create_file" if bad == "long_create" else "edit_file",
                        arguments=arguments,
                    ),
                ),
            )

        async def close(self) -> None:
            pass

    provider = InvalidProvider()
    agent, store, engine, workspace = await setup_agent(tmp_path, provider, FakeSandbox([]))
    try:
        with pytest.raises(CodingRunError, match="bounded native"):
            await agent.run(task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True)
        assert provider.calls == 2
        assert workspace.source_snapshot("convergence") == {"app.py": "value = 1\n"}
        assert not (await store.get_task("convergence")).tool_calls
    finally:
        await engine.dispose()


async def test_short_local_edit_and_explicit_ready_still_require_validation(tmp_path: Path) -> None:
    class LocalProvider:
        configured = True
        calls = 0

        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            self.calls += 1
            payload = json.loads(messages[-1].content)
            if self.calls == 1:
                assert payload["output_contract"]["argument_utf8_bytes"] == MAX_ARGUMENT_BYTES
                call = ToolCall(
                    id="edit",
                    name="edit_file",
                    arguments={"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
                )
            else:
                assert payload["source_files"] == {"app.py": "value = 2\n"}
                call = ToolCall(
                    id="ready", name="submit_code_ready", arguments={"summary": "Ready"}
                )
            return LLMResult(text="", model="script", usage=TokenUsage(2, 1, 3), tool_calls=(call,))

        async def close(self) -> None:
            pass

    sandbox = FakeSandbox([run_result(), run_result()])
    agent, _, engine, workspace = await setup_agent(tmp_path, LocalProvider(), sandbox)
    try:
        result = await agent.run(
            task_id="convergence", objective="Set value to 2", plan=[], snapshot_mode=True
        )
        assert result.model_calls == 2 and result.files_written == 1
        assert [r.action for r in sandbox.requests] == ["static_check", "run_pytest"]
        assert all(r.files == workspace.source_snapshot("convergence") for r in sandbox.requests)
    finally:
        await engine.dispose()


def test_snapshot_schemas_are_copied_without_changing_general_tools() -> None:
    from autoscholar.llm.models import ToolDefinition

    original = ToolDefinition(
        name="edit_file",
        description="Edit",
        parameters={
            "type": "object",
            "properties": {
                key: {"type": "string", "maxLength": 1000000}
                for key in ("path", "old_text", "new_text")
            },
        },
    )
    bounded = snapshot_tool(original)
    assert original.parameters["properties"]["new_text"]["maxLength"] == 1000000
    assert bounded.parameters["properties"]["new_text"]["maxLength"] == 1024
    assert snapshot_violation(ToolCall(id="x", name="delete_file", arguments={"path": "app.py"}))


async def test_bounded_edit_still_previews_complete_source_and_preserves_it_if_unsent(
    tmp_path: Path,
) -> None:
    class PreviewProvider:
        configured = True
        calls = 0

        def __init__(self) -> None:
            self.previews: list[dict[str, Any]] = []

        async def preflight(self, messages: Any, **kwargs: Any) -> None:
            payload = json.loads(messages[-1].content)
            if payload["source_files"]["app.py"] == "value = 22\n":
                self.previews.append(payload)
                assert kwargs["reserve_bytes"] == 1024
                assert kwargs["tool_choice"] == "required"
                raise LLMRequestRejected(message="Prospective complete source exceeds cap")

        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            self.calls += 1
            payload = json.loads(messages[-1].content)
            assert payload["source_files"] == {"app.py": "value = 1\n"}
            if self.calls == 1:
                call = ToolCall(
                    id="edit",
                    name="edit_file",
                    arguments={"path": "app.py", "old_text": "value = 1", "new_text": "value = 22"},
                )
            else:
                assert payload["recent_operations"][-1]["status"] == "failed"
                assert "file unchanged" in payload["recent_operations"][-1]["output"]
                call = ToolCall(
                    id="ready", name="submit_code_ready", arguments={"summary": "Ready"}
                )
            return LLMResult(text="", model="script", usage=TokenUsage(2, 1, 3), tool_calls=(call,))

        async def close(self) -> None:
            pass

    provider = PreviewProvider()
    sandbox = FakeSandbox([run_result(), run_result()])
    agent, store, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
    try:
        result = await agent.run(
            task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True
        )
        assert result.files_written == 0 and len(provider.previews) == 1
        assert workspace.source_snapshot("convergence") == {"app.py": "value = 1\n"}
        child = await store.get_task("convergence")
        assert child.tool_calls[0].status == "failed"
        assert child.tool_calls[0].error_code == "coding_snapshot_too_large"
        assert [request.action for request in sandbox.requests] == ["static_check", "run_pytest"]
    finally:
        await engine.dispose()
