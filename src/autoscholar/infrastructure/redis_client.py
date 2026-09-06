from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis


class RedisClient:
    """Async Redis connection manager."""

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def ping(self) -> bool:
        ping_result = await cast(Awaitable[bool], self._client.ping())
        return bool(ping_result)

    async def close(self) -> None:
        await self._client.aclose()
