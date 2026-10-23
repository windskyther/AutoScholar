"""Local request admission uses the real SDK and mock wire; never real API credentials."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager
from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.core.journal import current_journal
from autoscholar.evaluation.provider_transport import MAX_REQUEST_BYTES, GuardedTransport
from autoscholar.evaluation.repair_protocol import JSON_RULE, RepairLedger, RepairWorkflowProvider
from autoscholar.llm.errors import LLMRequestRejected
from autoscholar.llm.models import (
    AssistantToolCallMessage,
    ChatMessage,
    ConversationMessage,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
)
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_repository import LeaseLost
from tests.test_autonomous import workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_llm_provider import configured_settings
from tests.test_workflow_foundation import plan


def guard(tmp_path: Path, handler: Any) -> GuardedTransport:
    return GuardedTransport(
        provider="llm",
        endpoint="https://llm.example/v1/chat/completions",
        ledger=RepairLedger(tmp_path / "fake-grant.json", run_id="mock-only", protocol_version=4),
        output=tmp_path,
        secrets=("test-secret",),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(handler),
    )


def forbidden_send(request: httpx.Request) -> httpx.Response:
    raise AssertionError("A rejected request must not reach the delegate")


def reply(name: str, arguments: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "mock",
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
                                "id": "mock-call",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        },
    )


@pytest.mark.parametrize("early", [True, False], ids=["preflight", "late-sdk-guard"])
async def test_unsent_rejection_has_no_calls_tokens_receipts_or_pending_journal(
    tmp_path: Path, early: bool
) -> None:
    transport = guard(tmp_path, forbidden_send)
    events: list[tuple[str, str, bool]] = []

    async def hook(call_id: str, kind: str, starting: bool) -> None:
        events.append((call_id, kind, starting))

    budget = Budget(BudgetLimits(), used={"model_calls": 2, "total_tokens": 10})
    budget_token, journal_token = current_budget.set(budget), current_journal.set(hook)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            native = OpenAICompatibleProvider(
                configured_settings(),
                http_client=client,
                request_validator=transport.validate_payload if early else None,
            )
            provider = RepairWorkflowProvider(native, transport, "normal-baseline")
            metered = BudgetedLLM(provider)
            messages: list[ConversationMessage] = [
                ChatMessage(role="user", content="中文📚" * 5000)
            ]
            before = transport.ledger.path.read_bytes()
            if early:
                with pytest.raises(LLMRequestRejected):
                    await metered.preflight(messages)
                assert not events and provider.calls == 0 and transport.case_id is None
                assert budget.used == {"model_calls": 2, "total_tokens": 10}
            with pytest.raises(LLMRequestRejected) as caught:
                await metered.generate(messages)
            assert caught.value.dispatched is False
            assert not provider.failed and provider.calls == 0
            assert not provider.stage_calls and provider.memory_payloads == 0
            assert transport.case_id is None and not transport.ledger.attempts
            assert transport.ledger.path.read_bytes() == before
            assert not list(tmp_path.glob("request-*.json"))
            assert budget.used == {"model_calls": 2, "total_tokens": 10}
            assert [event[1:] for event in events] == (
                [("llm_local_rejected", False)]
                if early
                else [("llm", True), ("llm_local_rejected", False)]
            )
            if not early:
                assert events[0][0] == events[1][0] != ""
    finally:
        current_journal.reset(journal_token)
        current_budget.reset(budget_token)


def test_exact_utf8_forwarded_boundary_and_reserve_are_pure(tmp_path: Path) -> None:
    transport = guard(tmp_path, forbidden_send)
    payload: dict[str, Any] = {
        "model": "test",
        "messages": [{"role": "user", "content": '中文📚\n"'}],
    }
    capped = transport.validate_payload(payload)
    encoded = json.dumps(capped, ensure_ascii=False, allow_nan=False).encode()
    payload["messages"][0]["content"] += "x" * (MAX_REQUEST_BYTES - len(encoded))
    original = json.dumps(payload, ensure_ascii=False)
    accepted = transport.validate_payload(payload)
    assert len(json.dumps(accepted, ensure_ascii=False).encode()) == MAX_REQUEST_BYTES
    assert "max_tokens" not in payload and json.dumps(payload, ensure_ascii=False) == original
    with pytest.raises(LLMRequestRejected) as reserved:
        transport.validate_payload(payload, 1)
    assert reserved.value.request_bytes == MAX_REQUEST_BYTES
    payload["messages"][0]["content"] += "x"
    with pytest.raises(LLMRequestRejected):
        transport.validate_payload(payload)
    assert not transport.ledger.attempts and not list(tmp_path.glob("request-*.json"))


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"content": "test-secret"}, "llm_request_credentials_rejected"),
        ({"content": float("nan")}, "llm_request_invalid"),
        ({"content": object()}, "llm_request_invalid"),
    ],
)
def test_policy_rejections_are_safe_and_do_not_mutate_ledger(
    tmp_path: Path, payload: dict[str, Any], code: str
) -> None:
    transport = guard(tmp_path, forbidden_send)
    with pytest.raises(LLMRequestRejected) as caught:
        transport.validate_payload(payload)
    assert caught.value.code == code and caught.value.dispatched is False
    assert "test-secret" not in str(caught.value)
    assert not transport.ledger.attempts and not list(tmp_path.glob("request-*.json"))


async def test_preview_matches_exact_native_payload_and_transforms_only_once(
    tmp_path: Path,
) -> None:
    previews: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return reply("submit_code_ready", {"summary": "Ready"})

    transport = guard(tmp_path, handler)

    def validate(payload: dict[str, Any], reserve: int) -> None:
        previews.append(transport.validate_payload(payload, reserve))

    async with httpx.AsyncClient(transport=transport) as client:
        provider = RepairWorkflowProvider(
            OpenAICompatibleProvider(
                configured_settings(), http_client=client, request_validator=validate
            ),
            transport,
            "normal-baseline",
        )
        messages: list[ConversationMessage] = [
            ChatMessage(role="system", content="中文 system"),
            AssistantToolCallMessage(
                tool_calls=(
                    ToolCall(id="previous", name="read_file", arguments={"path": "中.py"}),
                ),
                reasoning_content="reasoning 中文",
            ),
            ToolResultMessage(tool_call_id="previous", content='中文\n"quoted"'),
        ]
        tools = [ToolDefinition(name="submit_code_ready", description="Ready", parameters={})]
        await provider.preflight(messages, tools=tools, tool_choice="submit_code_ready")
        assert provider.calls == 0 and not transport.ledger.attempts
        await provider.generate(messages, tools=tools, tool_choice="submit_code_ready")
        assert previews and all(preview == sent[0] for preview in previews)
        assert sent[0]["messages"][0]["content"].count(JSON_RULE) == 1
        assert messages[0].content == "中文 system"
        assert provider.calls == len(transport.ledger.attempts) == 1


@pytest.mark.parametrize("early", [True, False], ids=["preflight", "late-sdk-guard"])
async def test_durable_local_rejection_is_failed_without_phantom_usage_or_replay(
    tmp_path: Path, early: bool
) -> None:
    transport = guard(tmp_path, forbidden_send)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = RepairWorkflowProvider(
            OpenAICompatibleProvider(
                configured_settings(),
                http_client=client,
                request_validator=transport.validate_payload if early else None,
            ),
            transport,
            "normal-baseline",
        )
        service, engine = await workflow(
            tmp_path / "w",
            provider,
            FakeExperimentSandbox(),
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'workflow.sqlite'}",
        )
        durable = DurableService(service)
        try:
            task_id, _ = await durable.submit({"objective": "📚" * 9000}, "oversized")
            assert await durable.tick(task_id)
            _, job = await durable.repository.snapshot(task_id)
            assert job.status == "failed" and job.error_code == "llm_request_too_large"
            assert not job.pending_calls and job.usage.get("model_calls", 0) == 0
            assert job.usage.get("total_tokens", 0) == 0
            events = await durable.repository.history(task_id, "events")
            local = [e for e in events if e["kind"] == "local_request_rejected"]
            started = [e for e in events if e["kind"] == "call_started"]
            assert len(local) == 1 and local[0]["payload"]["dispatched"] is False
            assert len(started) == (0 if early else 1)
            assert not await DurableService(service).tick(task_id)
            assert not transport.ledger.attempts and provider.calls == 0
        finally:
            await engine.dispose()


@pytest.mark.parametrize("early", [True, False], ids=["preflight", "late-sdk-guard"])
async def test_coding_local_rejection_stops_before_review_or_replan(
    tmp_path: Path, early: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply("submit_task_plan", plan().model_dump())

    transport = guard(tmp_path, handler)
    original = transport.validate_payload

    def policy(payload: dict[str, Any], reserve: int = 0) -> dict[str, Any]:
        if any(t["function"]["name"] == "submit_code_ready" for t in payload.get("tools", [])):
            raise LLMRequestRejected(message="Local coding input policy rejected; not sent")
        return original(payload, reserve)

    # Exercise both preflight and the late SDK-wrapped rejection with the same policy.
    monkeypatch.setattr(transport, "validate_payload", policy)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = RepairWorkflowProvider(
            OpenAICompatibleProvider(
                configured_settings(),
                http_client=client,
                request_validator=policy if early else None,
            ),
            transport,
            "normal-baseline",
        )
        service, engine = await workflow(tmp_path / "w", provider, FakeExperimentSandbox())
        durable = DurableService(service)
        try:
            task_id, _ = await durable.submit({"objective": "Compare models"}, "coding")
            assert await durable.tick(task_id)  # Planner is one completed, known call.
            assert await durable.tick(task_id)  # Terminal local coding rejection.
            _, job = await durable.repository.snapshot(task_id)
            assert job.status == "failed" and not job.pending_calls
            assert job.error_code == "llm_request_too_large"
            assert job.usage["model_calls"] == provider.calls == len(transport.ledger.attempts) == 1
            assert job.usage["total_tokens"] == 5
            steps = await service.workflows.history(task_id, "steps")
            assert len(steps) == 1 and steps[0]["status"] == "failed"
            assert provider.stage_calls == {"submit_task_plan": 1} and not provider.failed
            assert not await durable.tick(task_id)
        finally:
            await engine.dispose()


@pytest.mark.parametrize("failure", ["connect", "timeout", "invalid-response", "large-response"])
async def test_dispatched_failures_keep_unknown_usage_and_pending_journal(
    tmp_path: Path, failure: str
) -> None:
    dispatched = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal dispatched
        dispatched += 1
        if failure == "connect":
            raise httpx.ConnectError("unknown remote outcome", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown remote outcome", request=request)
        return httpx.Response(200, content=b"x" * (140000 if failure == "large-response" else 10))

    transport = guard(tmp_path, handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = RepairWorkflowProvider(
            OpenAICompatibleProvider(
                configured_settings(),
                http_client=client,
                request_validator=transport.validate_payload,
            ),
            transport,
            "normal-baseline",
        )
        service, engine = await workflow(tmp_path / "w", provider, FakeExperimentSandbox())
        durable = DurableService(service)
        try:
            task_id, _ = await durable.submit({"objective": "Compare models"}, failure)
            assert await durable.tick(task_id)
            _, job = await durable.repository.snapshot(task_id)
            assert job.status == "recovery_required" and job.pending_calls
            assert job.usage["model_calls"] == dispatched == provider.calls == 1
            assert transport.ledger.attempts[0]["state"] == "unknown"
            events = await durable.repository.history(task_id, "events")
            assert not [e for e in events if e["kind"] == "local_request_rejected"]
            assert not await durable.tick(task_id)
        finally:
            await engine.dispose()


async def test_rejection_cannot_clear_another_pending_call_or_close_twice(tmp_path: Path) -> None:
    from tests.test_agent_runner import ScriptedProvider

    service, engine = await workflow(tmp_path, ScriptedProvider([]), FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "journal")
        claimed = await durable.repository.claim(durable.owner, 30)
        assert claimed
        generation = claimed[1]
        args = (task_id, durable.owner, generation)
        await durable.repository.journal(
            *args, "one", "llm", True, {"model_calls": 3, "total_tokens": 7}, 1
        )
        await durable.repository.journal(
            *args, "", "llm_local_rejected", False, {"model_calls": 3, "total_tokens": 7}, 1
        )
        _, pending = await durable.repository.snapshot(task_id)
        assert pending.pending_calls == {"one": "llm"} and pending.usage["model_calls"] == 3
        with pytest.raises(LeaseLost):
            await durable.repository.journal(*args, "wrong", "llm_local_rejected", False, {}, 1)
        await durable.repository.journal(
            *args, "one", "llm_local_rejected", False, {"model_calls": 2, "total_tokens": 7}, 1
        )
        _, closed = await durable.repository.snapshot(task_id)
        assert not closed.pending_calls and closed.usage == {"model_calls": 2, "total_tokens": 7}
        with pytest.raises(LeaseLost):
            await durable.repository.journal(*args, "one", "llm_local_rejected", False, {}, 1)
    finally:
        await engine.dispose()


def test_workspace_preview_preserves_disk_and_matches_utf8_newlines_and_edit(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(tmp_path)
    root = workspace.initialize("preview")
    before = sorted(root.rglob("*"))
    content = "中文\r\nline2\rline3\n"
    sources, record = workspace.preview_text("preview", "nested/中.py", content)
    assert sorted(root.rglob("*")) == before
    written = workspace.write_text("preview", "nested/中.py", content)
    assert sources == workspace.source_snapshot("preview")
    assert (record.path, record.sha256, record.size_bytes) == (
        written.path,
        written.sha256,
        written.size_bytes,
    )
    prior = (root / "source/nested/中.py").read_bytes()
    edited, edit_record = workspace.preview_edit("preview", "nested/中.py", "line2", "新行\r\n")
    assert (root / "source/nested/中.py").read_bytes() == prior
    applied = workspace.edit_text("preview", "nested/中.py", "line2", "新行\r\n")
    assert applied.sha256 == edit_record.sha256 and edited == workspace.source_snapshot("preview")
    with pytest.raises(WorkspaceError, match="exactly once"):
        workspace.preview_edit("preview", "nested/中.py", "absent", "new")
    with pytest.raises(WorkspaceError, match="safe relative"):
        workspace.preview_text("preview", "../escape.py", "bad")
    with pytest.raises(WorkspaceError, match="already exists"):
        workspace.preview_text("preview", "nested/中.py", "bad")
    limited = WorkspaceManager(tmp_path, max_files=1)
    with pytest.raises(WorkspaceError, match="file-count"):
        limited.preview_text("preview", "second.py", "bad")


@pytest.mark.parametrize("operation", ["create_file", "edit_file"])
async def test_oversized_source_write_is_rejected_then_ready_runs_mandatory_checks(
    tmp_path: Path, operation: str
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return reply("submit_task_plan", plan().model_dump())
        if len(requests) == 2:
            source = json.loads(payload["messages"][-1]["content"])["source_files"]
            arguments = (
                {"path": "extra.py", "content": "#" + "中文" * 6000}
                if operation == "create_file"
                else {
                    "path": "train.py",
                    "old_text": source["train.py"],
                    "new_text": source["train.py"] + "\n#" + "中文" * 6000,
                }
            )
            return reply(operation, arguments)
        if len(requests) == 3:
            previous = json.loads(requests[1]["messages"][-1]["content"])
            current = json.loads(payload["messages"][-1]["content"])
            assert current["source_files"] == previous["source_files"]
            assert current["recent_operations"][-1]["status"] == "failed"
            assert "file unchanged" in current["recent_operations"][-1]["output"]
            return reply(
                "submit_code_ready", {"summary": "Existing seeded code satisfies requirements"}
            )
        assert len(requests) == 4
        return reply("submit_review", {"status": "PASS"})

    transport = guard(tmp_path, handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = RepairWorkflowProvider(
            OpenAICompatibleProvider(
                configured_settings(),
                http_client=client,
                request_validator=transport.validate_payload,
            ),
            transport,
            "normal-baseline",
        )
        sandbox = FakeExperimentSandbox()
        service, engine = await workflow(tmp_path / "w", provider, sandbox)
        try:
            result = await service.run("Compare models")
            assert result.task.status == "succeeded", result.task.error_message
            assert (
                provider.calls
                == len(transport.ledger.attempts)
                == result.task.metrics["model_calls"]
                == 4
            )
            assert result.task.metrics["total_tokens"] == 20 and not provider.failed
            assert provider.corrections == 0
            steps = await service.workflows.history(result.task.id, "steps")
            coding_id = next(s["child_task_id"] for s in steps if s["step_id"] == "code")
            task = await service.tasks.get_task(coding_id)
            assert task
            traces = task.tool_calls
            rejected = [t for t in traces if t.tool_name == operation]
            assert len(rejected) == 1 and rejected[0].status == "failed"
            assert rejected[0].error_code == "coding_snapshot_too_large"
            assert {"static_check", "run_pytest"}.issubset(
                {t.tool_name for t in traces if t.status == "succeeded"}
            )
            assert task.metrics["files_written"] == 0
        finally:
            await engine.dispose()
