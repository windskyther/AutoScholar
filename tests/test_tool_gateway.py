import asyncio
from typing import Any

import pytest

from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.core.journal import current_journal
from autoscholar.tool_platform.gateway import (
    NativeBackend,
    ToolContract,
    ToolGateway,
    ToolGatewayError,
)

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
CONTRACT = ToolContract("echo", SCHEMA, SCHEMA)


async def echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments


async def test_native_contract_and_one_journal_boundary() -> None:
    events: list[tuple[str, str, bool]] = []

    async def journal(operation_id: str, kind: str, starting: bool) -> None:
        events.append((operation_id, kind, starting))

    token = current_journal.set(journal)
    try:
        gateway = ToolGateway(NativeBackend([CONTRACT], {"echo": echo}), [CONTRACT])
        assert await gateway.invoke("echo", {"text": "中文 → MCP"}) == {"text": "中文 → MCP"}
        assert events[0][0] == events[1][0]
        assert [(item[1], item[2]) for item in events] == [("mcp:echo", True), ("mcp:echo", False)]
    finally:
        current_journal.reset(token)


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("shell", {"text": "x"}, "tool_not_allowed"),
        ("echo", {"text": 12}, "tool_arguments_invalid"),
        ("echo", {"text": "x", "task_id": "victim"}, "tool_arguments_invalid"),
    ],
)
async def test_invalid_call_never_reaches_backend(
    name: str, args: dict[str, Any], code: str
) -> None:
    async def forbidden(_: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("Must not dispatch")

    gateway = ToolGateway(NativeBackend([CONTRACT], {"echo": forbidden}), [CONTRACT])
    with pytest.raises(ToolGatewayError, match=code) as error:
        await gateway.invoke(name, args)
    assert not error.value.uncertain


async def test_changed_or_external_reference_schema_is_rejected() -> None:
    backend = NativeBackend([CONTRACT], {"echo": echo})
    gateway = ToolGateway(backend, [CONTRACT])
    await gateway.invoke("echo", {"text": "first"})
    backend.contracts = [ToolContract("echo", SCHEMA | {"title": "changed"}, SCHEMA)]
    with pytest.raises(ToolGatewayError, match="tool_contract_changed"):
        await gateway.invoke("echo", {"text": "second"})
    backend.contracts = [ToolContract("echo", {"$ref": "http://localhost/secret"}, SCHEMA)]
    with pytest.raises(ToolGatewayError, match="tool_contract_invalid"):
        await gateway.invoke("echo", {"text": "third"})


@pytest.mark.parametrize("failure", ["timeout", "cancel", "malformed"])
async def test_dispatched_failure_stays_pending_and_does_not_retry(failure: str) -> None:
    calls = 0
    events: list[bool] = []
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def handler(_: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        started.set()
        try:
            if failure == "malformed":
                return {"wrong": "shape"}
            await asyncio.Event().wait()
            return {}
        finally:
            cleaned.set()

    async def journal(operation_id: str, kind: str, starting: bool) -> None:
        events.append(starting)

    token = current_journal.set(journal)
    try:
        gateway = ToolGateway(
            NativeBackend([CONTRACT], {"echo": handler}), [CONTRACT], timeout_seconds=0.1
        )
        task = asyncio.create_task(gateway.invoke("echo", {"text": "x"}))
        await started.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ToolGatewayError) as error:
                await task
            assert error.value.uncertain
        assert calls == 1 and events == [True] and cleaned.is_set()
    finally:
        current_journal.reset(token)


async def test_budget_deadline_cannot_be_extended_by_gateway() -> None:
    from autoscholar.core.budget import BudgetExceeded

    token = current_budget.set(Budget(BudgetLimits(wall_seconds=1), started=0))
    try:
        gateway = ToolGateway(NativeBackend([CONTRACT], {"echo": echo}), [CONTRACT])
        with pytest.raises(BudgetExceeded):
            await gateway.invoke("echo", {"text": "x"})
    finally:
        current_budget.reset(token)


async def test_unavailable_before_dispatch_does_not_create_pending_call() -> None:
    events: list[bool] = []

    async def journal(operation_id: str, kind: str, starting: bool) -> None:
        events.append(starting)

    token = current_journal.set(journal)
    try:
        gateway = ToolGateway(NativeBackend([], {}), [CONTRACT])
        with pytest.raises(ToolGatewayError, match="tool_unavailable") as error:
            await gateway.invoke("echo", {"text": "x"})
        assert not error.value.uncertain and not events
    finally:
        current_journal.reset(token)


async def test_gateway_does_not_double_count_runner_budget() -> None:
    budget = Budget(BudgetLimits(), used={"tool_calls": 1, "search_queries": 1})
    token = current_budget.set(budget)
    try:
        gateway = ToolGateway(NativeBackend([CONTRACT], {"echo": echo}), [CONTRACT])
        await gateway.invoke("echo", {"text": "x"})
        assert budget.used == {"tool_calls": 1, "search_queries": 1}
    finally:
        current_budget.reset(token)


async def test_transport_rejects_redirects_and_bounds_wire_bytes() -> None:
    import httpx2

    from autoscholar.tool_platform.transport import BoundedHTTPTransport, BoundedStream

    transport = BoundedHTTPTransport()
    await transport.transport.aclose()
    transport.transport = httpx2.MockTransport(  # type: ignore[assignment]
        lambda request: httpx2.Response(307, headers={"location": "http://example.org/stolen"})
    )
    try:
        with pytest.raises(ToolGatewayError, match="tool_redirect_rejected"):
            await transport.handle_async_request(httpx2.Request("POST", "http://localhost/mcp"))
    finally:
        await transport.aclose()

    class OversizedStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b"x" * 1_048_576
            yield b"x"

    stream = BoundedStream(OversizedStream())
    with pytest.raises(ToolGatewayError, match="tool_output_too_large"):
        async for _ in stream:
            pass
