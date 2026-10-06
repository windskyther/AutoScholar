"""Real SQL event cursors and safe read-only streaming; all providers are fake."""

import asyncio
import importlib
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from anyio import Event, create_task_group, sleep
from fastapi import Request
from pydantic import SecretStr

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.api.workbench_events import event_stream
from autoscholar.api.workbench_models import WorkflowEventPage
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.main import create_app
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.rag.repository import KnowledgeRepository
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency

AUTH = {"Authorization": "Bearer public-event-test-token"}


@pytest.fixture
async def context(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, DurableService]]:
    provider = ScriptedProvider([])
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(
        tmp_path,
        provider,
        sandbox,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite'}",
    )
    durable = DurableService(service)
    root, _ = await durable.submit({"objective": "公开事件验收"}, "events-test")
    await service.tasks.create_task(task_id="legacy", objective="Legacy", mode="research")
    async with service.tasks.session_factory() as session:
        session.add(
            AgentTaskRow(
                id="child",
                parent_task_id=root,
                objective="Child",
                status="succeeded",
                mode="research",
            )
        )
        for kind, payload in (
            (
                "unit_started",
                {
                    "stage": "executor",
                    "version": 1,
                    "owner": "PRIVATE_OWNER",
                    "prompt": "PRIVATE_PROMPT",
                },
            ),
            (
                "checkpoint_saved",
                {"stage": "writer", "status": "running", "sequence": 2, "reason": "PRIVATE_REASON"},
            ),
            (
                "approval_decided",
                {
                    "decision": "approve",
                    "reason": "PRIVATE_SECRET",
                    "operation_sha256": "PRIVATE_HASH",
                },
            ),
            ("PRIVATE_UNKNOWN_KIND", {"status": "succeeded", "secret": "PRIVATE_SECRET"}),
        ):
            await durable.repository.event(session, root, kind, **payload)
        await session.commit()
    app = create_app(
        Settings(_env_file=None, experiment_api_token=SecretStr("public-event-test-token")),  # type: ignore[call-arg]
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        knowledge_repository=KnowledgeRepository(service.tasks.session_factory),
        workspace_manager=service.workspace,
        sandbox_executor=sandbox,
        research_services=[],
    )
    app.state.durable_service = durable
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Fixture-Task": root},
        ) as client:
            yield client, durable
    finally:
        try:
            assert not provider.calls and not sandbox.requests
        finally:
            await engine.dispose()


@pytest.mark.parametrize("endpoint", ["events", "stream"])
async def test_event_reads_require_header_auth(
    context: tuple[httpx.AsyncClient, DurableService], endpoint: str
) -> None:
    client, _ = context
    path = f"/workbench/tasks/{client.headers['X-Fixture-Task']}/{endpoint}"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, params={"token": "public-event-test-token"})).status_code == 401


@pytest.mark.parametrize("task", ["child", "missing"])
async def test_event_reads_only_accept_roots(
    context: tuple[httpx.AsyncClient, DurableService], task: str
) -> None:
    client, _ = context
    for endpoint in ("events", "stream"):
        assert (
            await client.get(f"/workbench/tasks/{task}/{endpoint}", headers=AUTH)
        ).status_code == 404


async def test_safe_projection_and_cursor_pages(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    client, _ = context
    path = f"/workbench/tasks/{client.headers['X-Fixture-Task']}/events"
    latest = await client.get(path, headers=AUTH, params={"limit": 2})
    assert latest.json()["has_older"] and not latest.json()["has_more"]
    assert [item["sequence"] for item in latest.json()["items"]] == [4, 5]
    assert latest.json()["items"][0]["payload"] == {"decision": "approve"}
    assert latest.json()["items"][1]["kind"] == "other"
    assert latest.json()["items"][1]["payload"] == {}
    assert "PRIVATE" not in latest.text
    first = await client.get(path, headers=AUTH, params={"after": 0, "limit": 2})
    assert [row["sequence"] for row in first.json()["items"]] == [1, 2]
    assert first.json()["has_more"] and not first.json()["has_older"]
    assert first.json()["items"][1]["payload"] == {"stage": "executor", "version": 1}
    following = await client.get(path, headers=AUTH, params={"after": first.json()["next_cursor"]})
    assert [row["sequence"] for row in following.json()["items"]] == [3, 4, 5]
    older = await client.get(path, headers=AUTH, params={"before": 4, "limit": 2})
    assert [row["sequence"] for row in older.json()["items"]] == [2, 3]
    assert older.json()["has_older"]
    assert first.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize(
    "params",
    [
        {"after": -1},
        {"before": 0},
        {"limit": 101},
        {"after": 0, "before": 2},
        {"after": 9007199254740992},
    ],
)
async def test_event_cursor_validation(
    context: tuple[httpx.AsyncClient, DurableService], params: dict[str, int]
) -> None:
    client, _ = context
    path = f"/workbench/tasks/{client.headers['X-Fixture-Task']}/events"
    assert (await client.get(path, headers=AUTH, params=params)).status_code == 422
    assert (await client.get(path, headers=AUTH, params={"after": 6})).status_code == 409


async def test_sequence_rollback_and_concurrent_writers(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    client, durable = context
    task = client.headers["X-Fixture-Task"]
    async with durable.repository.sessions() as session:
        await durable.repository.event(session, task, "pause", status="paused")
        await session.flush()
        await session.rollback()

    async def write() -> None:
        async with durable.repository.sessions() as session:
            await durable.repository.event(session, task, "budget_saved")
            await session.commit()

    await asyncio.gather(*(write() for _ in range(6)))
    data = (await client.get(f"/workbench/tasks/{task}/events?after=5", headers=AUTH)).json()
    assert [row["sequence"] for row in data["items"]] == list(range(6, 12))
    assert data["next_cursor"] == 11


async def test_concurrent_idempotent_submission_catches_event_autoflush_conflicts(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    _, durable = context
    results = await asyncio.gather(
        *(
            durable.submit({"objective": "公开并发提交验收"}, "same-concurrent-key")
            for _ in range(5)
        )
    )
    assert len({task for task, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    task = results[0][0]
    async with durable.repository.sessions() as session:
        job_count = await session.scalar(sa.select(sa.func.count()).select_from(WorkflowJobRow))
        task_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(AgentTaskRow)
            .where(AgentTaskRow.objective == "公开并发提交验收")
        )
    assert job_count == 2  # The fixture root plus one new task, no duplicate jobs.
    assert task_count == 1  # Rolled-back losing requests must not leave orphan roots.
    events = await WorkbenchRepository(durable.repository.sessions).events(task, after=0)
    assert [event.sequence for event in events.items] == [1]


@pytest.mark.parametrize("cursor", ["2", "5"])
async def test_terminal_stream_replays_then_ends(
    context: tuple[httpx.AsyncClient, DurableService], cursor: str
) -> None:
    client, durable = context
    task = client.headers["X-Fixture-Task"]
    async with durable.repository.sessions() as session:
        row = await session.get(AgentTaskRow, task)
        assert row is not None
        row.status = "succeeded"
        await session.commit()
    response = await client.get(
        f"/workbench/tasks/{task}/stream", headers=AUTH | {"Last-Event-ID": cursor}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.text.startswith("event: ready\n") and '"reason":"terminal"' in response.text
    assert "PRIVATE" not in response.text
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == list(range(int(cursor) + 1, 6))


@pytest.mark.parametrize(
    "header,query,status",
    [("bad", "", 422), ("9007199254740992", "", 422), ("6", "", 409), ("2", "?after=3", 422)],
)
async def test_stream_cursor_rejected_before_stream_headers(
    context: tuple[httpx.AsyncClient, DurableService], header: str, query: str, status: int
) -> None:
    client, _ = context
    response = await client.get(
        f"/workbench/tasks/{client.headers['X-Fixture-Task']}/stream{query}",
        headers=AUTH | {"Last-Event-ID": header},
    )
    assert response.status_code == status and "application/json" in response.headers["content-type"]


async def test_legacy_history_has_no_live_execution(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    client, _ = context
    response = await client.get("/workbench/tasks/legacy/stream", headers=AUTH)
    assert '"durable":false' in response.text and '"reason":"unsupported"' in response.text


class Connection:
    disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def test_live_stream_heartbeat_new_commits_rotation_and_disconnect(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    client, durable = context
    task = client.headers["X-Fixture-Task"]
    repository = WorkbenchRepository(durable.repository.sessions)
    first = await repository.events(task, after=5)
    connection = Connection()
    stream = event_stream(
        repository,
        cast(Request, connection),
        first,
        poll_seconds=0.001,
        heartbeat_seconds=0,
        duration_seconds=0.04,
    )
    assert "event: ready" in await anext(stream)
    async with durable.repository.sessions() as session:
        await durable.repository.event(session, task, "budget_saved")
        await session.commit()
    output = "".join([chunk async for chunk in stream])
    assert ": heartbeat" in output and "id: 6\n" in output and '"reason":"rotate"' in output
    connection.disconnected = True
    disconnected = event_stream(repository, cast(Request, connection), first)
    assert "event: ready" in await anext(disconnected)
    with pytest.raises(StopAsyncIteration):
        await anext(disconnected)


async def test_stream_storage_failure_never_leaks_exception(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    client, durable = context
    repository = WorkbenchRepository(durable.repository.sessions)
    first = await repository.events(client.headers["X-Fixture-Task"], after=5)
    with patch.object(
        repository,
        "events",
        AsyncMock(side_effect=AppError(status_code=503, code="private", message="PRIVATE_SECRET")),
    ):
        output = "".join(
            [
                chunk
                async for chunk in event_stream(
                    repository, cast(Request, Connection()), first, poll_seconds=0
                )
            ]
        )
    assert '"code":"workbench_stream_unavailable"' in output and "PRIVATE" not in output


async def test_stream_disconnect_during_sql_read_finishes_session_cleanup(
    context: tuple[httpx.AsyncClient, DurableService],
) -> None:
    client, durable = context
    repository = WorkbenchRepository(durable.repository.sessions)
    first = await repository.events(client.headers["X-Fixture-Task"], after=5)
    entered, closed = Event(), Event()
    original = repository.events

    async def slow_read(task_id: str, *, after: int, limit: int) -> WorkflowEventPage:
        entered.set()
        await sleep(0.03)  # Cancel while a database read is in progress.
        result = await original(task_id, after=after, limit=limit)
        closed.set()  # Original returns only after the SQL session is closed.
        return result

    async def consume() -> None:
        async for _ in event_stream(repository, cast(Request, Connection()), first, poll_seconds=0):
            pass

    with patch.object(repository, "events", slow_read):
        async with create_task_group() as group:
            group.start_soon(consume)
            await entered.wait()
            group.cancel_scope.cancel()
    assert closed.is_set()


def test_event_migration_backfill_roundtrip_and_postgres_sql() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261006_0013_workbench_event_sequence"
    )
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    tasks = sa.Table("agent_tasks", metadata, sa.Column("id", sa.String(36), primary_key=True))
    events = sa.Table(
        "workflow_events",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.id"), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(tasks.insert(), [{"id": "a"}, {"id": "b"}])
            connection.execute(
                events.insert(),
                [
                    {
                        "id": event_id,
                        "task_id": "a",
                        "kind": "submitted",
                        "payload": {"public": event_id},
                        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                    for event_id in ("z", "a")
                ],
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert [
                tuple(row)
                for row in connection.execute(
                    sa.text("SELECT id, sequence FROM workflow_events ORDER BY sequence")
                )
            ] == [("a", 1), ("z", 2)]
            assert [
                tuple(row)
                for row in connection.execute(
                    sa.text("SELECT id, event_sequence FROM agent_tasks ORDER BY id")
                )
            ] == [("a", 2), ("b", 0)]
            before = connection.execute(sa.select(events).order_by(events.c.id)).all()
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.execute(sa.select(events).order_by(events.c.id)).all() == before
            assert "event_sequence" not in {
                col["name"] for col in sa.inspect(connection).get_columns("agent_tasks")
            }
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        output = io.StringIO()
        with Operations.context(
            MigrationContext.configure(
                dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
            )
        ):
            migration.upgrade()
        assert "row_number()" in output.getvalue() and "CREATE UNIQUE INDEX" in output.getvalue()
    finally:
        engine.dispose()
