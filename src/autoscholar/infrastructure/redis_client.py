import json
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis


class RedisClient:
    """Async Redis connection manager."""

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def ping(self) -> bool:
        ping_result = await cast(Awaitable[bool], self._client.ping())
        return bool(ping_result)

    async def get_json(self, key: str) -> Any | None:
        value = await self._client.get(key)
        if value is None:
            return None
        return json.loads(str(value))

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self._client.set(key, encoded, ex=ttl_seconds)

    async def close(self) -> None:
        await self._client.aclose()
