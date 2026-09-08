from qdrant_client import AsyncQdrantClient


class Qdrant:
    """Async Qdrant connection manager."""

    def __init__(self, url: str, *, api_key: str | None = None) -> None:
        self.client = AsyncQdrantClient(
            url=url,
            api_key=api_key,
            check_compatibility=False,
        )

    async def ping(self) -> bool:
        await self.client.get_collections()
        return True

    async def close(self) -> None:
        await self.client.close()
