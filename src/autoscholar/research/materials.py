"""Optional bounded public-source extraction. Never fetch arbitrary URLs locally."""

import hashlib
import ipaddress
import json
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from autoscholar.core.budget import consume
from autoscholar.core.journal import external_operation
from autoscholar.research.service import JSONCache


class ReadResult(BaseModel):
    text: str = ""
    truncated: bool = False
    error: str | None = None
    cache_hit: bool = False


def public_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        if (
            url.scheme not in {"http", "https"}
            or not host
            or url.username
            or url.password
            or url.port not in {None, 80, 443}
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and "\\" not in value and not any(c.isspace() for c in value)
    except ValueError:
        return False


class MaterialReader:
    def __init__(
        self,
        *,
        api_key: str | None,
        cache: JSONCache | None = None,
        ttl_seconds: int = 86400,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.key, self.cache, self.ttl = api_key, cache, ttl_seconds
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def read(self, url: str) -> ReadResult:
        if not self.key or not public_url(url):
            return ReadResult(error="extraction_unavailable")
        cache_key = "autoscholar:materials:v1:" + hashlib.sha256(url.encode()).hexdigest()
        if self.cache is not None and self.ttl > 0:
            try:
                cached = await self.cache.get_json(cache_key)
                if isinstance(cached, dict):
                    result = ReadResult.model_validate(cached)
                    if result.text and len(result.text) <= 12000:
                        return result.model_copy(update={"cache_hit": True})
            except Exception:
                pass
        consume("tool_calls")
        consume("search_queries")
        # No whole-request retries: an interrupted extraction remains journaled
        # as uncertain, and a completed failed URL remains visibly summary-only.
        async with external_operation("search:tavily_extract"):
            async with self.client.stream(
                "POST",
                "https://api.tavily.com/extract",
                timeout=30,
                headers={"Authorization": "Bearer " + self.key},
                json={
                    "urls": [url],
                    "extract_depth": "basic",
                    "format": "markdown",
                    "include_images": False,
                    "include_usage": True,
                },
            ) as response:
                if response.status_code != 200:
                    return ReadResult(error="extraction_http_error")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        return ReadResult(error="extraction_too_large")
            try:
                payload = json.loads(content)
                results = payload.get("results", [])
                item = next(
                    (
                        item
                        for item in results
                        if isinstance(item, dict)
                        and str(item.get("url", "")).rstrip("/") == url.rstrip("/")
                    ),
                    None,
                )
                if not item or not isinstance(item.get("raw_content"), str):
                    return ReadResult(error="source_not_extracted")
                raw = item["raw_content"].strip()
            except (ValueError, AttributeError, TypeError):
                return ReadResult(error="extraction_invalid_response")
            result = ReadResult(
                text=raw[:12000], truncated=len(raw) > 12000, error=None if raw else "source_empty"
            )
        if self.cache is not None and self.ttl > 0 and result.text:
            with suppress(Exception):
                await self.cache.set_json(cache_key, result.model_dump(), ttl_seconds=self.ttl)
        return result

    async def close(self) -> None:
        await self.client.aclose()
