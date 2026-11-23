"""Public Redis-only cross-process fixture; never initializes application settings."""

import asyncio
import json
import sys
from uuid import uuid4

from redis.asyncio import Redis

from autoscholar.core.admission import RequestLimit
from autoscholar.infrastructure.admission import RedisAdmissionStore


async def run(url: str, namespace: str) -> None:
    client: Redis = Redis.from_url(url, decode_responses=True)
    store = RedisAdmissionStore(client, namespace=namespace)
    try:
        results = await asyncio.gather(
            *(
                store.acquire(
                    "agent",
                    str(uuid4()),
                    RequestLimit(per_minute=100, concurrent=3, deadline_seconds=10),
                    30,
                )
                for _ in range(20)
            )
        )
        print(json.dumps({"accepted": sum(result.accepted for result in results)}))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], sys.argv[2]))
