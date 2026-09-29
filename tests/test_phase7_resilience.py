"""Deterministic reproductions for interrupted worker supervision and HTTP disconnects."""

import asyncio
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest

from autoscholar.llm import LLMResult
from autoscholar.orchestration.durable import DurableService
from autoscholar.sandbox.manager import create_manager_app
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_durable_workflow import drain
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_sandbox import FakeSandbox


class WaitingProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__(script())
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.executing: asyncio.Task[Any] | None = None

    async def generate(self, *args: Any, **kwargs: Any) -> LLMResult:
        self.executing = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("Waiting provider must not finish normally")


@pytest.mark.parametrize("failure", ["connection", "hanging", "blocked_cleanup"])
async def test_heartbeat_failure_cancels_and_joins_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    provider = WaitingProvider()
    service, engine = await workflow(
        tmp_path / "workspace",
        provider,
        FakeExperimentSandbox(),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'heartbeat.sqlite'}",
    )
    durable = DurableService(service, lease_seconds=1)

    async def broken_heartbeat(*args: Any, **kwargs: Any) -> str:
        if failure == "connection":
            raise ConnectionError("Simulated database disconnect")
        try:
            await asyncio.Event().wait()
        finally:
            if failure == "blocked_cleanup":
                # Simulate asyncpg rollback waiting for a paused database. Execution
                # must be cancelled independently, not after this cleanup completes.
                await provider.cancelled.wait()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(durable.repository, "heartbeat", broken_heartbeat)
    try:
        task_id, _ = await durable.submit({"objective": "Heartbeat fault"}, failure)
        started = asyncio.get_running_loop().time()
        with pytest.raises((ConnectionError, TimeoutError)):
            await asyncio.wait_for(durable.tick(task_id), 2)
        assert provider.started.is_set()
        assert provider.cancelled.is_set(), "Heartbeat failure left execution running"
        assert asyncio.get_running_loop().time() - started < 1.5
        _, job = await durable.repository.snapshot(task_id)
        assert job.status == "recovery_required"
        assert job.pending_calls and job.usage["model_calls"] == 1
        assert not await DurableService(service).tick(task_id)
    finally:
        if provider.executing is not None and not provider.executing.done():
            provider.executing.cancel()
            await asyncio.gather(provider.executing, return_exceptions=True)
        await engine.dispose()


async def test_manager_disconnect_cancels_execution_and_waits_for_cleanup() -> None:
    class HeldSandbox(FakeSandbox):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.cleaned = asyncio.Event()

        async def run(self, request: Any) -> Any:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                self.cleaned.set()

    sandbox = HeldSandbox()
    app = create_manager_app(sandbox)
    request_body = b'{"task_id":"disconnect","action":"run_pytest","files":{}}'
    sent_body = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await sandbox.started.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(dict(message))

    running = asyncio.create_task(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/internal/v1/run",
                "raw_path": b"/internal/v1/run",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234),
                "server": ("test", 80),
                "root_path": "",
            },
            receive,
            send,
        )
    )
    try:
        done, _ = await asyncio.wait({running}, timeout=0.5)
        assert done, "Disconnected client left sandbox running"
        await running
        assert sandbox.cleaned.is_set()
        assert (
            next(
                message["status"]
                for message in messages
                if message["type"] == "http.response.start"
            )
            == 499
        )
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_repeated_cancel_preserves_completed_result_metrics(tmp_path: Path) -> None:
    service, engine = await workflow(tmp_path, ScriptedProvider(script()), FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "completed")
        assert await drain(durable, task_id) == "succeeded"
        before = await service.tasks.get_task(task_id)
        assert before and before.metrics["artifact_count"] == 10
        assert await durable.repository.control(task_id, "cancel") == "succeeded"
        after = await service.tasks.get_task(task_id)
        assert after and before.metrics == after.metrics
        assert before.answer == after.answer
    finally:
        await engine.dispose()
