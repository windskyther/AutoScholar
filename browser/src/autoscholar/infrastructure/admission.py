"""Redis TIME and Lua keep rates and expiring concurrency leases atomic across APIs."""

import math
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis

from autoscholar.core.admission import Admission, RequestLimit

ACQUIRE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60000)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local retry = 0
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
    local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    retry = math.max(retry, tonumber(first[2]) + 60000 - now)
end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
    local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    retry = math.max(retry, tonumber(first[2]) - now)
end
if retry > 0 then return {0, retry} end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[4]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], 60000)
redis.call('PEXPIRE', KEYS[2], ARGV[4])
return {1, 0}
"""
RENEW = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""


class RedisAdmissionStore:
    def __init__(self, client: Redis, *, namespace: str = "autoscholar:admission:v1") -> None:
        self.client, self.namespace = client, namespace

    def _keys(self, category: str) -> tuple[str, str]:
        # Stable across token rotation; hash tag also permits Redis Cluster Lua execution.
        prefix = f"{self.namespace}:{{{category}}}"
        return prefix + ":rate", prefix + ":leases"

    async def acquire(
        self, category: str, lease: str, limit: RequestLimit, ttl_seconds: float
    ) -> Admission:
        result: Any = await cast(
            Awaitable[Any],
            self.client.eval(
                ACQUIRE,
                2,
                *self._keys(category),
                lease,
                limit.per_minute,
                limit.concurrent,
                math.ceil(ttl_seconds * 1000),
            ),
        )
        return Admission(bool(result[0]), max(1, math.ceil(int(result[1]) / 1000)))

    async def renew(self, category: str, lease: str, ttl_seconds: float) -> bool:
        return bool(
            await cast(
                Awaitable[Any],
                self.client.eval(
                    RENEW, 1, self._keys(category)[1], lease, math.ceil(ttl_seconds * 1000)
                ),
            )
        )

    async def release(self, category: str, lease: str) -> None:
        await self.client.zrem(self._keys(category)[1], lease)
