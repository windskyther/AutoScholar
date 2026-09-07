from typing import Any

import httpx
import pytest

from autoscholar.research import (
    RedisResearchCache,
    ResearchSearchService,
    SearchProviderError,
    SearchResult,
    SemanticScholarSearchProvider,
    TavilySearchProvider,
)


def async_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def no_sleep(_: float) -> None:
    return None


async def test_tavily_maps_bounded_results_without_exposing_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer private-key"
        assert b'"search_depth":"basic"' in request.content
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "LoRA",
                        "url": "https://example.com/lora",
                        "content": "Low-rank adaptation summary",
                        "score": 0.91,
                    },
                    {"title": "Missing content", "url": "https://example.com/empty"},
                ]
            },
        )

    provider = TavilySearchProvider(
        api_key="private-key",
        client=async_client(handler),
        retry_delays=(),
    )

    results = await provider.search("LoRA", limit=5)

    assert len(results) == 1
    assert results[0].provider == "tavily"
    assert results[0].relevance == 0.91
    assert "private-key" not in repr(results)
    await provider.close()


async def test_tavily_requires_configuration() -> None:
    provider = TavilySearchProvider(api_key=None, retry_delays=())

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("LoRA", limit=5)

    assert exc_info.value.code == "tavily_not_configured"
    await provider.close()


async def test_semantic_scholar_maps_paper_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "s2-key"
        assert request.url.params["fields"].startswith("title,url,abstract")
        assert request.url.params["query"] == "Low Rank Adaptation"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "paperId": "paper-1",
                        "title": "LoRA: Low-Rank Adaptation",
                        "url": "https://www.semanticscholar.org/paper/paper-1",
                        "abstract": "We propose low-rank adaptation.",
                        "authors": [{"name": "Alice"}, {"name": "Bob"}],
                        "year": 2021,
                        "externalIds": {"DOI": "10.1/lora"},
                    }
                ]
            },
        )

    provider = SemanticScholarSearchProvider(
        api_key="s2-key",
        client=async_client(handler),
        retry_delays=(),
        minimum_interval_seconds=0,
    )

    results = await provider.search("Low-Rank Adaptation", limit=3)

    assert results == [
        SearchResult(
            source_type="paper",
            provider="semantic_scholar",
            title="LoRA: Low-Rank Adaptation",
            url="https://doi.org/10.1/lora",
            content="We propose low-rank adaptation.",
            authors=("Alice", "Bob"),
            year=2021,
            external_id="10.1/lora",
            relevance=1.0,
        )
    ]
    await provider.close()


async def test_provider_retries_rate_limit_and_honors_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"results": []})

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    provider = TavilySearchProvider(
        api_key="key",
        client=async_client(handler),
        retry_delays=(0.1,),
        sleep=record_sleep,
    )

    assert await provider.search("LoRA", limit=2) == []
    assert calls == 2
    assert delays == [0.1]
    await provider.close()


class MemoryCacheClient:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.writes = 0

    async def get_json(self, key: str) -> Any | None:
        return self.data.get(key)

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        assert ttl_seconds == 60
        self.data[key] = value
        self.writes += 1


async def test_search_service_normalizes_and_caches_results() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "DoRA",
                        "url": "https://example.com/dora",
                        "content": "Weight-decomposed adaptation",
                    }
                ]
            },
        )

    cache_client = MemoryCacheClient()
    provider = TavilySearchProvider(
        api_key="key",
        client=async_client(handler),
        retry_delays=(),
    )
    service = ResearchSearchService(
        provider=provider,
        cache=RedisResearchCache(cache_client, ttl_seconds=60),
    )

    first = await service.search("  DoRA   paper  ", limit=10)
    second = await service.search("DoRA paper", limit=5)

    assert first.query == "DoRA paper"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls == 1
    assert cache_client.writes == 1
    await service.close()


async def test_cache_failure_does_not_block_search() -> None:
    class BrokenCacheClient:
        async def get_json(self, key: str) -> Any | None:
            del key
            raise ConnectionError("redis unavailable")

        async def set_json(self, key: str, value: Any, *, ttl_seconds: int) -> None:
            del key, value, ttl_seconds
            raise ConnectionError("redis unavailable")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    service = ResearchSearchService(
        provider=TavilySearchProvider(
            api_key="key",
            client=async_client(handler),
            retry_delays=(),
        ),
        cache=RedisResearchCache(BrokenCacheClient()),
    )

    response = await service.search("QLoRA")

    assert response.results == ()
    await service.close()
