import json
import time
from typing import Any

from sqlalchemy import text

from autoscholar.coding.sandbox import (
    SandboxError,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.desktop.storage import DesktopDatabase


class Cache:
    def __init__(self, database: DesktopDatabase) -> None:
        self.database = database

    async def get_json(self, key: str) -> Any:
        async with self.database.session_factory() as session:
            payload = await session.scalar(
                text("SELECT payload FROM desktop_cache WHERE key=:key AND expires>:now"),
                {"key": key, "now": time.time()},
            )
            return json.loads(payload) if payload else None

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        async with self.database.session_factory.begin() as session:
            now = time.time()
            await session.execute(
                text("DELETE FROM desktop_cache WHERE expires<=:now"), {"now": now}
            )
            await session.execute(
                text("INSERT OR REPLACE INTO desktop_cache VALUES (:key,:payload,:expires)"),
                {"key": key, "payload": json.dumps(value), "expires": now + ttl_seconds},
            )


class DisabledService:
    async def ping(self) -> bool:
        return False

    async def close(self) -> None:
        pass


class DisabledSandbox(DisabledService):
    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        raise SandboxError("sandbox_unavailable", "Desktop research cannot execute code")

    async def health(self) -> SandboxHealth:
        return SandboxHealth(status="error", engine=False, image=False, mnist_dataset=False)
