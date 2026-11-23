"""Offline PostgreSQL aggregation check in a temporary, isolated schema.

Run inside the API container. No model, search or task-execution endpoints are called.
"""

import asyncio
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Table, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.core.config import Settings
from autoscholar.llm.models import TokenUsage
from autoscholar.llm.usage import LOCAL_ZONE, ModelUsageRow, UsageStore


async def main() -> None:
    schema = "usage_acceptance_" + uuid4().hex[:12]
    assert re.fullmatch(r"usage_acceptance_[0-9a-f]{12}", schema)
    engine = create_async_engine(Settings().database_url)
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with scoped.begin() as connection:
            usage_table = ModelUsageRow.__table__
            assert isinstance(usage_table, Table)
            await connection.run_sync(usage_table.create)
        store = UsageStore(async_sessionmaker(scoped, expire_on_commit=False))
        calls = [
            await store.start("api-a", "Reader", "shared", None),
            await store.start("api-b", "Writer", "shared", None),
            await store.start("api-b", "Writer", "other", None),
        ]
        await store.finish(calls[0], "succeeded", TokenUsage(10, 20, 30))
        await store.finish(calls[1], "failed", TokenUsage(5, 5, 10))
        summary = await store.summary(days=7)
        assert summary.totals.calls == 3 and summary.totals.total_tokens == 40
        assert summary.totals.unknown_usage_calls == 1
        assert sum(item.total_tokens for item in summary.daily) == 40
        by_model = await store.summary(group_by="model", days=7)
        assert by_model.totals == summary.totals
        assert {item.label: item.total_tokens for item in by_model.groups} == {
            "shared": 40,
            "other": 0,
        }
        selected = await store.summary(api_id="api-b", model="shared", days=7)
        assert selected.totals.calls == 1 and selected.totals.total_tokens == 10
        assert summary.daily[-1].date == datetime.now(UTC).astimezone(LOCAL_ZONE).date().isoformat()
        print("postgres_usage_aggregation: passed; external_api_calls: 0")
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
