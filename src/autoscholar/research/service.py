import hashlib
import json
from typing import Any, Protocol

import structlog

from autoscholar.research.models import SearchResponse, SearchResult, SourceType
from autoscholar.research.providers import BaseHTTPResearchProvider

logger = structlog.get_logger(__name__)


class JSONCache(Protocol):
    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int) -> None: ...


class RedisResearchCache:
    def __init__(self, client: JSONCache, *, ttl_seconds: int = 86_400) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._ttl_seconds > 0

    async def get(self, key: str) -> list[SearchResult] | None:
        if not self.enabled:
            return None
        try:
            payload = await self._client.get_json(key)
            if not isinstance(payload, list):
                return None
            return [SearchResult.from_dict(item) for item in payload if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("research_cache_read_failed", exception_type=type(exc).__name__)
            return None

    async def set(self, key: str, results: list[SearchResult]) -> None:
        if not self.enabled:
            return
        try:
            await self._client.set_json(
                key,
                [result.to_dict() for result in results],
                ttl_seconds=self._ttl_seconds,
            )
        except Exception as exc:
            logger.warning("research_cache_write_failed", exception_type=type(exc).__name__)


class ResearchSearchService:
    def __init__(
        self,
        *,
        provider: BaseHTTPResearchProvider,
        cache: RedisResearchCache | None = None,
    ) -> None:
        self.provider = provider
        self._cache = cache

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def source_type(self) -> SourceType:
        return self.provider.source_type

    @property
    def configured(self) -> bool:
        return self.provider.configured

    async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        normalized = " ".join(query.split())
        if not normalized or len(normalized) > 400:
            raise ValueError("Search query must contain between 1 and 400 characters")
        bounded_limit = min(max(limit, 1), 5)
        cache_key = self._cache_key(normalized, bounded_limit)
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return SearchResponse(
                    provider=self.name,
                    source_type=self.source_type,
                    query=normalized,
                    results=tuple(cached),
                    cache_hit=True,
                )
        results = await self.provider.search(normalized, limit=bounded_limit)
        if self._cache is not None:
            await self._cache.set(cache_key, results)
        return SearchResponse(
            provider=self.name,
            source_type=self.source_type,
            query=normalized,
            results=tuple(results),
        )

    def _cache_key(self, query: str, limit: int) -> str:
        payload = json.dumps(
            {"provider": self.name, "query": query.casefold(), "limit": limit},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"autoscholar:research:v1:{digest}"

    async def close(self) -> None:
        await self.provider.close()
