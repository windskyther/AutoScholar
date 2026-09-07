import asyncio
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from autoscholar.research.models import SearchResult, SourceType

Sleep = Callable[[float], Awaitable[None]]


class SearchProviderError(Exception):
    def __init__(self, *, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class BaseHTTPResearchProvider:
    name: str
    source_type: SourceType

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
        retry_delays: tuple[float, ...] = (0.5, 1.0),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._timeout_seconds = timeout_seconds
        self._retry_delays = retry_delays
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        raise NotImplementedError

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    url,
                    timeout=self._timeout_seconds,
                    **kwargs,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 == attempts:
                    raise SearchProviderError(
                        code=f"{self.name}_unavailable",
                        message=f"{self.name} search is unavailable",
                        retryable=True,
                    ) from exc
                await self._sleep(self._retry_delays[attempt])
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt + 1 == attempts:
                    raise SearchProviderError(
                        code=f"{self.name}_upstream_error",
                        message=f"{self.name} search returned HTTP {response.status_code}",
                        retryable=True,
                    )
                delay = self._retry_after(response) or self._retry_delays[attempt]
                await self._sleep(delay)
                continue
            if response.status_code in {401, 403}:
                raise SearchProviderError(
                    code=f"{self.name}_authentication_failed",
                    message=f"{self.name} search authentication failed",
                )
            if response.status_code >= 400:
                raise SearchProviderError(
                    code=f"{self.name}_request_failed",
                    message=f"{self.name} search returned HTTP {response.status_code}",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SearchProviderError(
                    code=f"{self.name}_invalid_response",
                    message=f"{self.name} search returned invalid JSON",
                ) from exc
            if not isinstance(payload, dict):
                raise SearchProviderError(
                    code=f"{self.name}_invalid_response",
                    message=f"{self.name} search returned an invalid response",
                )
            return payload
        raise AssertionError("search retry loop exited unexpectedly")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(max(float(raw), 0.0), 10.0)
        except ValueError:
            try:
                delay = parsedate_to_datetime(raw).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                return None
            return min(max(delay, 0.0), 10.0)


class TavilySearchProvider(BaseHTTPResearchProvider):
    name = "tavily"
    source_type: SourceType = "web"

    def __init__(self, *, api_key: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key.strip() if api_key else ""

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        if not self.configured:
            raise SearchProviderError(
                code="tavily_not_configured",
                message="Tavily search is not configured",
            )
        payload = await self._request_json(
            "POST",
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "query": query,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
                "max_results": limit,
            },
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise SearchProviderError(
                code="tavily_invalid_response",
                message="Tavily search returned an invalid result list",
            )
        results: list[SearchResult] = []
        for item in raw_results[:limit]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            content = str(item.get("content") or "").strip()[:2_000]
            if not title or not url or not content:
                continue
            score = item.get("score")
            results.append(
                SearchResult(
                    source_type="web",
                    provider=self.name,
                    title=title,
                    url=url,
                    content=content,
                    relevance=float(score) if isinstance(score, int | float) else None,
                )
            )
        return results


class SemanticScholarSearchProvider(BaseHTTPResearchProvider):
    name = "semantic_scholar"
    source_type: SourceType = "paper"

    def __init__(
        self,
        *,
        api_key: str | None,
        minimum_interval_seconds: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key.strip() if api_key else ""
        self._minimum_interval_seconds = minimum_interval_seconds
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        headers = {"x-api-key": self._api_key} if self._api_key else {}
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            if self._last_request_at and elapsed < self._minimum_interval_seconds:
                await self._sleep(self._minimum_interval_seconds - elapsed)
            try:
                payload = await self._request_json(
                    "GET",
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    headers=headers,
                    params={
                        "query": query,
                        "limit": limit,
                        "fields": (
                            "title,url,abstract,authors,year,externalIds,openAccessPdf"
                        ),
                    },
                )
            finally:
                self._last_request_at = time.monotonic()
        raw_results = payload.get("data")
        if not isinstance(raw_results, list):
            raise SearchProviderError(
                code="semantic_scholar_invalid_response",
                message="Semantic Scholar returned an invalid result list",
            )
        results: list[SearchResult] = []
        for index, item in enumerate(raw_results[:limit]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("abstract") or "").strip()[:2_000]
            paper_id = str(item.get("paperId") or "").strip()
            external_ids = item.get("externalIds")
            doi = external_ids.get("DOI") if isinstance(external_ids, dict) else None
            url = f"https://doi.org/{doi}" if doi else str(item.get("url") or "").strip()
            authors_payload = item.get("authors")
            authors = tuple(
                str(author.get("name")).strip()
                for author in authors_payload
                if isinstance(author, dict) and author.get("name")
            ) if isinstance(authors_payload, list) else ()
            if not title or not url or not content:
                continue
            year = item.get("year")
            results.append(
                SearchResult(
                    source_type="paper",
                    provider=self.name,
                    title=title,
                    url=url,
                    content=content,
                    authors=authors,
                    year=int(year) if isinstance(year, int) else None,
                    external_id=str(doi or paper_id) or None,
                    relevance=max(0.5, 1.0 - index * 0.1),
                )
            )
        return results
