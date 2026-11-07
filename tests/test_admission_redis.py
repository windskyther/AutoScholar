"""Isolated service tests; env vars must point at disposable, credential-free fixtures."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import Table, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from autoscholar.api.governance import RequestGovernance
from autoscholar.core.admission import RequestLimit, default_request_limits
from autoscholar.core.audit import DatabaseAuditSink, SecurityAuditRow
from autoscholar.core.config import Settings
from autoscholar.infrastructure.admission import RedisAdmissionStore
from autoscholar.infrastructure.database import Database
from autoscholar.infrastructure.redis_client import RedisClient
from autoscholar.main import create_app
from tests.api_credentials import AUTH, TOKEN_SECRET
from tests.test_chat import SuccessfulProvider
from tests.test_health import FakeDependency, FakeSandbox

REDIS_URL = os.getenv("PHASE11C_REDIS_URL")
DATABASE_URL = os.getenv("PHASE11C_DATABASE_URL")


@pytest.mark.skipif(not REDIS_URL, reason="requires an isolated Phase 11C Redis fixture")
async def test_real_redis_cross_process_atomic_limits_and_expiring_leases() -> None:
    assert REDIS_URL
    namespace = "phase11c:" + uuid4().hex
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    processes: list[asyncio.subprocess.Process] = []
    try:
        for _ in range(2):
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tests.integration.phase11_admission_worker",
                    REDIS_URL,
                    namespace,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
        outputs = await asyncio.wait_for(
            asyncio.gather(*(process.communicate() for process in processes)), 45
        )
        assert all(process.returncode == 0 for process in processes), outputs
        assert sum(json.loads(stdout)["accepted"] for stdout, _ in outputs) == 3

        store = RedisAdmissionStore(client, namespace=namespace)
        limit = RequestLimit(per_minute=1, concurrent=1, deadline_seconds=10)
        assert (await store.acquire("upload", "first", limit, 0.1)).accepted
        denied = await store.acquire("upload", "second", limit, 0.1)
        assert not denied.accepted and 1 <= denied.retry_after <= 60
        await asyncio.sleep(0.15)
        assert not await store.renew("upload", "first", 1)
        # Expired concurrency does not clear the rate budget.
        assert not (await store.acquire("upload", "third", limit, 1)).accepted

        limit = RequestLimit(per_minute=100, concurrent=1, deadline_seconds=10)
        assert (await store.acquire("llm", "first", limit, 0.1)).accepted
        assert await store.renew("llm", "first", 1)
        await asyncio.sleep(0.15)
        assert not (await store.acquire("llm", "second", limit, 1)).accepted
        await store.release("llm", "first")
        assert (await store.acquire("llm", "second", limit, 1)).accepted
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        keys = [key async for key in client.scan_iter(match=namespace + ":*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.mark.skipif(not DATABASE_URL, reason="requires an isolated Phase 11C PostgreSQL fixture")
def test_postgres_full_migration_chain_and_new_revision_roundtrip() -> None:
    assert DATABASE_URL
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")
    command.downgrade(config, "20261006_0013")
    command.upgrade(config, "head")

    async def verify() -> None:
        engine = create_async_engine(DATABASE_URL)
        try:
            async with engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("security_audit")
                )
                assert {column["name"] for column in columns} >= {
                    "request_id",
                    "actor",
                    "operation_id",
                    "phase",
                    "usage",
                    "action",
                }
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.skipif(
    not DATABASE_URL or not REDIS_URL, reason="requires isolated Phase 11C services"
)
async def test_real_http_shared_admission_and_durable_audit_without_suppliers(
    tmp_path: Path,
) -> None:
    assert DATABASE_URL and REDIS_URL
    database = Database(DATABASE_URL)
    redis = RedisClient(REDIS_URL)
    namespace = "phase11c-http:" + uuid4().hex
    first, second = SuccessfulProvider(), SuccessfulProvider()
    limits = default_request_limits()
    limits["llm"].per_minute = 1
    config = Settings(
        model_encryption_key=TOKEN_SECRET,
        api_request_limits=limits,
        document_storage_path=tmp_path / "documents",
        workspace_root=tmp_path / "workspaces",
    )
    sink = DatabaseAuditSink(database.session_factory)
    apps = [
        create_app(
            config,
            database=database,
            redis=redis,
            qdrant=FakeDependency(),
            llm_provider=provider,
            sandbox_executor=FakeSandbox(),
            research_services=[],
            governance=RequestGovernance(
                RedisAdmissionStore(redis.client, namespace=namespace), sink
            ),
        )
        for provider in (first, second)
    ]
    request_id = "phase11c-" + uuid4().hex
    try:
        async with database._engine.begin() as connection:
            await connection.run_sync(
                cast(Table, SecurityAuditRow.__table__).create, checkfirst=True
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[0]), base_url="http://test"
        ) as a:
            response = await a.post(
                "/chat",
                headers=AUTH | {"X-Request-ID": request_id},
                json={"message": "private-body-not-for-audit"},
            )
            assert response.status_code == 200, response.text
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[1]), base_url="http://test"
        ) as b:
            response = await b.post("/chat", headers=AUTH, json={"message": "public"})
            assert response.status_code == 429, response.text
        assert len(first.messages) == 1 and not second.messages
        async with database.session_factory() as session:
            rows = (
                await session.scalars(
                    select(SecurityAuditRow).where(SecurityAuditRow.request_id == request_id)
                )
            ).all()
            assert {row.phase for row in rows} == {
                "admitted",
                "tool_dispatch",
                "tool_completed",
                "completed",
            }
            assert "private-body-not-for-audit" not in str([row.__dict__ for row in rows])
            completed = next(row for row in rows if row.phase == "completed")
            assert completed.usage is not None and completed.usage["total_tokens"] > 0
    finally:
        keys = [key async for key in redis.client.scan_iter(match=namespace + ":*")]
        if keys:
            await redis.client.delete(*keys)
        await redis.close()
        await database.close()
