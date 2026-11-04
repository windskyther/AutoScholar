import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.types import ASGIApp

from autoscholar.api.access import APIAuthenticationMiddleware
from autoscholar.api.governance import (
    GovernanceError,
    RequestGovernance,
    RequestGovernanceMiddleware,
)
from autoscholar.core.admission import Admission, RequestLimit, default_request_limits
from autoscholar.core.audit import (
    AuditEvent,
    DatabaseAuditSink,
    SecurityAuditRow,
    current_security_audit,
)
from autoscholar.core.config import Settings
from autoscholar.core.journal import current_journal, external_operation
from tests.api_credentials import AUTH, TOKEN_SECRET
from tests.governance import MemoryAdmission, MemoryAudit


def application(
    *,
    admission: MemoryAdmission | None = None,
    audit: MemoryAudit | None = None,
    **settings: object,
) -> tuple[FastAPI, MemoryAdmission, MemoryAudit, list[str]]:
    app = FastAPI()
    config = Settings(experiment_api_token=TOKEN_SECRET, **settings)  # type: ignore[arg-type]
    store, sink, calls = admission or MemoryAdmission(), audit or MemoryAudit(), []
    app.state.settings = config
    app.state.governance = RequestGovernance(store, sink)

    @app.post("/chat")
    async def chat(request: Request) -> dict[str, object]:
        await request.body()
        calls.append("dispatch")
        return {"task_id": str(uuid4()), "usage": None}

    app.add_middleware(
        RequestGovernanceMiddleware, settings=config, governance=app.state.governance
    )
    app.add_middleware(APIAuthenticationMiddleware, settings=config)
    return app, store, sink, calls


def client(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
    )


async def test_shared_rate_window_and_no_dispatch_after_denial() -> None:
    clock = [0.0]
    store = MemoryAdmission(lambda: clock[0])
    limits = default_request_limits()
    limits["llm"].per_minute = 1
    first, _, audit, calls = application(admission=store, api_request_limits=limits)
    second, _, _, other = application(admission=store, api_request_limits=limits)
    async with client(first) as a, client(second) as b:
        assert (await a.post("/chat", json={})).status_code == 200
        response = await b.post("/chat", json={})
        assert response.status_code == 429 and int(response.headers["Retry-After"]) >= 1
        assert calls == ["dispatch"] and other == []
        clock[0] = 60
        assert (await b.post("/chat", json={})).status_code == 200
    assert len(audit.events) == 2 and audit.events[-1].usage is None
    assert not any(store.leases.values())


async def test_shared_concurrency_released_on_cancellation() -> None:
    app, store, _, _ = application()
    entered, release = asyncio.Event(), asyncio.Event()

    @app.post("/agent/run")
    async def blocking() -> dict[str, str]:
        entered.set()
        await release.wait()
        return {"status": "done"}

    async with client(app) as api:
        running = asyncio.create_task(api.post("/agent/run", json={}))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await api.post("/agent/run", json={})).status_code == 429
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not store.leases["agent"]
        release.set()
        assert (await api.post("/agent/run", json={})).status_code == 200


@pytest.mark.parametrize("failure", ["redis", "audit"])
async def test_backend_failure_blocks_dispatch(failure: str) -> None:
    class BrokenAdmission(MemoryAdmission):
        async def acquire(
            self, category: str, lease: str, limit: RequestLimit, ttl_seconds: float
        ) -> Admission:
            raise OSError("SYNTHETIC_SECRET")

    class BrokenAudit(MemoryAudit):
        async def write(self, event: AuditEvent) -> None:
            raise OSError("SYNTHETIC_SECRET")

    app, _, _, calls = application(
        admission=BrokenAdmission() if failure == "redis" else None,
        audit=BrokenAudit() if failure == "audit" else None,
    )
    async with client(app) as api:
        response = await api.post("/chat", json={})
    assert response.status_code == 503 and not calls
    assert "SYNTHETIC_SECRET" not in response.text


async def test_audit_completion_failure_keeps_admission_and_reports_unknown() -> None:
    class FailingCompletion(MemoryAudit):
        async def write(self, event: AuditEvent) -> None:
            if event.phase == "completed":
                raise OSError
            await super().write(event)

    app, _, sink, calls = application(audit=FailingCompletion())
    async with client(app) as api:
        response = await api.post("/chat", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_outcome_unknown"
    assert calls == ["dispatch"]
    assert [event.phase for event in sink.events] == ["admitted", "interrupted"]
    assert sink.events[0].operation_id == sink.events[1].operation_id


@pytest.mark.parametrize("declared", [False, True])
async def test_oversized_body_no_dispatch_even_with_forged_length(declared: bool) -> None:
    app, store, _, calls = application(api_json_max_bytes=1024)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 700
        yield b"y" * 700

    async with client(app) as api:
        response = await api.post(
            "/chat", content=chunks(), headers={"content-length": "1"} if declared else {}
        )
    assert response.status_code == 413 and not calls
    assert not store.leases["llm"]


async def test_slow_body_times_out_before_dispatch() -> None:
    app, store, _, calls = application(api_body_idle_seconds=0.02)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"first"
        await asyncio.sleep(0.1)
        yield b"last"

    async with client(app) as api:
        response = await api.post("/chat", content=chunks())
    assert response.status_code == 408 and not calls and not store.leases["llm"]


async def test_request_deadline_releases_lease_and_preserves_pending_journal() -> None:
    limits = default_request_limits()
    limits["agent"].deadline_seconds = 0.03
    app, store, sink, _ = application(api_request_limits=limits)
    journal: list[tuple[str, bool]] = []

    async def hook(operation: str, kind: str, starting: bool) -> None:
        journal.append((operation, starting))

    @app.post("/agent/run")
    async def unknown() -> None:
        token = current_journal.set(hook)
        try:
            async with external_operation("llm"):
                await asyncio.sleep(1)
        finally:
            current_journal.reset(token)

    async with client(app) as api:
        response = await api.post("/agent/run", json={})
    assert response.status_code == 504 and not store.leases["agent"]
    assert len(journal) == 1 and journal[0][1] is True
    assert any(event.phase == "tool_dispatch" for event in sink.events)
    assert not any(event.phase == "tool_completed" for event in sink.events)
    assert current_security_audit.get() is None


async def test_lost_lease_stops_an_active_request() -> None:
    class LostLease(MemoryAdmission):
        async def renew(self, category: str, lease: str, ttl_seconds: float) -> bool:
            return False

    app, store, _, _ = application(admission=LostLease(), api_lease_seconds=3)
    cancelled = asyncio.Event()

    @app.post("/agent/run")
    async def slow() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async with client(app) as api:
        response = await api.post("/agent/run", json={})
    assert response.status_code == 503 and cancelled.is_set() and not store.leases["agent"]


async def test_unknown_usage_stays_null_and_audit_excludes_request_text() -> None:
    app, _, sink, _ = application()
    async with client(app) as api:
        response = await api.post(
            "/chat?api_key=private-query",
            json={"prompt": "private-body"},
            headers={"Idempotency-Key": "private-idempotency"},
        )
        assert response.status_code == 200
        denial = await api.post("/chat", headers={"Authorization": "Bearer private-token"})
        assert denial.status_code == 401
    encoded = json.dumps([asdict(event) for event in sink.events])
    for secret in ("private-query", "private-body", "private-idempotency", "private-token"):
        assert secret not in encoded
    completed = next(event for event in sink.events if event.phase == "completed")
    assert completed.task_id == response.json()["task_id"] and completed.usage is None


async def test_response_cap_prevents_successful_mutation_response() -> None:
    app, store, sink, _ = application(api_response_max_bytes=1024)

    @app.post("/large")
    async def large() -> dict[str, str]:
        return {"text": "x" * 2048}

    async with client(app) as api:
        response = await api.post("/large", json={})
    assert response.status_code == 502 and not store.leases["write"]
    assert sink.events[-1].outcome == "response_too_large"


async def test_append_only_database_audit_survives_reopen(tmp_path: Path) -> None:
    url = "sqlite+aiosqlite:///" + (tmp_path / "audit.db").as_posix()
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(cast(Table, SecurityAuditRow.__table__).create)
    event = AuditEvent(
        id=str(uuid4()),
        request_id="public",
        actor="fingerprint",
        category="submit",
        method="POST",
        phase="admitted",
        outcome="pending",
    )
    await DatabaseAuditSink(async_sessionmaker(engine)).write(event)
    await engine.dispose()
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            rows = (await session.scalars(select(SecurityAuditRow))).all()
            assert len(rows) == 1 and rows[0].id == event.id and rows[0].usage is None
            assert (
                await session.scalar(
                    select(SecurityAuditRow.id).where(SecurityAuditRow.usage.is_(None))
                )
                == event.id
            )
    finally:
        await engine.dispose()


def test_limits_cannot_be_partially_disabled() -> None:
    with pytest.raises(ValidationError):
        Settings(api_request_limits={})


async def test_multipart_total_overhead_is_bounded_before_dispatch() -> None:
    app, store, _, calls = application(document_max_bytes=1024, api_multipart_overhead_bytes=1024)

    @app.post("/projects/public/documents")
    async def upload() -> None:
        calls.append("upload")

    async with client(app) as api:
        response = await api.post(
            "/projects/public/documents",
            files={"file": ("public.pdf", b"%PDF-" + b"x" * 3000, "application/pdf")},
        )
    assert response.status_code == 413 and not calls and not store.leases["upload"]


async def test_sse_idle_closes_without_replaying_and_releases_connection() -> None:
    app, store, sink, _ = application(api_sse_idle_seconds=0.02, api_lease_seconds=3)
    calls = []

    @app.get("/tasks/public/stream")
    async def stream() -> StreamingResponse:
        calls.append("stream")

        async def events() -> AsyncIterator[bytes]:
            yield b": ready\n\n"
            await asyncio.sleep(10)

        return StreamingResponse(events(), media_type="text/event-stream")

    async with client(app) as api:
        with pytest.raises(GovernanceError) as exc:
            await api.get("/tasks/public/stream")
    assert exc.value.code == "stream_idle_timeout"
    assert calls == ["stream"] and not store.leases["sse"]
    assert sink.events[-1].outcome == "stream_idle_timeout"


async def test_production_factory_has_no_implicit_offline_bypass() -> None:
    from autoscholar.main import create_app
    from tests.test_chat import SuccessfulProvider
    from tests.test_health import FakeDependency, FakeSandbox

    provider = SuccessfulProvider()
    app = create_app(
        Settings(experiment_api_token=TOKEN_SECRET),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        sandbox_executor=FakeSandbox(),
    )
    async with client(app) as api:
        response = await api.post("/chat", json={"message": "public"})
    assert response.status_code == 503 and not provider.messages


async def test_worker_audit_failure_prevents_external_dispatch(tmp_path: Path) -> None:
    from autoscholar.orchestration.durable import DurableService
    from tests.test_agent_runner import ScriptedProvider
    from tests.test_autonomous import script, workflow
    from tests.test_experiment_service import FakeExperimentSandbox

    class BrokenAudit(MemoryAudit):
        async def write(self, event: AuditEvent) -> None:
            raise OSError("synthetic-secret")

    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    durable = DurableService(service, audit=BrokenAudit())
    try:
        task_id, _ = await durable.submit({"objective": "public", "mode": "autonomous"}, "public")
        await durable.tick(task_id)
        assert not provider.calls
        _, job = await durable.repository.snapshot(task_id)
        assert job.status == "failed" and not job.pending_calls
    finally:
        await engine.dispose()
