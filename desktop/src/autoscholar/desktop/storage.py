"""Desktop-only SQLite schema and single-process admission policy."""

import math
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import URL, Connection

from autoscholar.agent.database_models import Base
from autoscholar.core.admission import Admission, RequestLimit
from autoscholar.infrastructure.database import Database


class DesktopDatabase(Database):
    def __init__(self, path: Path) -> None:
        super().__init__(URL.create("sqlite+aiosqlite", database=str(path)).render_as_string())
        event.listen(self._engine.sync_engine, "connect", self._configure)
        event.listen(self._engine.sync_engine, "before_cursor_execute", self._reserve_write)

    @staticmethod
    def _reserve_write(
        connection: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        # SQLite omits FOR UPDATE from compiled SQL. Reserve a writer before reading
        # the row, so task controls, lease renewals and competing claims serialize.
        compiled = context.compiled
        if compiled is None or getattr(compiled.statement, "_for_update_arg", None) is None:
            return
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            cursor.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _configure(connection: Any, _: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")

    async def initialize(self) -> None:
        async with self._engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            version = (await connection.exec_driver_sql("PRAGMA user_version")).scalar_one()
            if version == 0:
                await connection.run_sync(Base.metadata.create_all)
                await connection.exec_driver_sql(
                    "CREATE TABLE desktop_cache (key TEXT PRIMARY KEY, payload TEXT, expires REAL)"
                )
                await connection.exec_driver_sql("PRAGMA user_version=1")
            elif version != 1:
                raise ValueError("Unsupported desktop database version")
            await connection.commit()


class LocalAdmission:
    """All operations run synchronously on the API's single asyncio loop."""

    def __init__(self) -> None:
        self.starts: dict[str, deque[float]] = defaultdict(deque)
        self.leases: dict[str, dict[str, float]] = defaultdict(dict)

    async def acquire(
        self, category: str, lease: str, limit: RequestLimit, ttl_seconds: float
    ) -> Admission:
        now = time.monotonic()
        starts, leases = self.starts[category], self.leases[category]
        while starts and starts[0] <= now - 60:
            starts.popleft()
        for expired in [key for key, end in leases.items() if end <= now]:
            del leases[expired]
        if len(starts) >= limit.per_minute:
            return Admission(False, max(1, math.ceil(starts[0] + 60 - now)))
        if len(leases) >= limit.concurrent:
            return Admission(False)
        starts.append(now)
        leases[lease] = now + ttl_seconds
        return Admission(True)

    async def renew(self, category: str, lease: str, ttl_seconds: float) -> bool:
        now = time.monotonic()
        if self.leases[category].get(lease, 0) <= now:
            return False
        self.leases[category][lease] = now + ttl_seconds
        return True

    async def release(self, category: str, lease: str) -> None:
        self.leases[category].pop(lease, None)
