"""Pinned modern MCP protocol over Streamable HTTP, without implicit native fallback."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import RequestParamsMeta

from autoscholar.tool_platform.gateway import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ToolConnection,
    ToolGatewayError,
    ToolReply,
)


class BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > MAX_MESSAGE_BYTES:
                raise ToolGatewayError("tool_output_too_large", uncertain=True)
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class BoundedHTTPTransport(httpx2.AsyncBaseTransport):
    def __init__(self) -> None:
        self.transport = httpx2.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.transport.handle_async_request(request)
        if response.is_redirect:
            await response.aclose()
            raise ToolGatewayError("tool_redirect_rejected")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise ToolGatewayError("tool_compression_rejected")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=BoundedStream(cast(httpx2.AsyncByteStream, response.stream)),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self.transport.aclose()


class MCPConnection:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def discover(self) -> dict[str, dict[str, Any]]:
        tools: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(8):
            page = await self.client.list_tools(cursor=cursor, cache_mode="bypass")
            for tool in page.tools:
                if tool.name in tools or len(tools) >= 64:
                    raise ToolGatewayError("tool_catalog_invalid")
                tools[tool.name] = tool.input_schema
            cursor = page.next_cursor
            if not cursor:
                return tools
        raise ToolGatewayError("tool_catalog_invalid")

    async def call(
        self, name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> ToolReply:
        result = await self.client.call_tool(
            name, arguments, meta=cast(RequestParamsMeta, {"autoscholar": context})
        )
        return ToolReply(result.structured_content or {}, is_error=result.is_error)


class MCPBackend:
    def __init__(self, url: str, token: str, *, timeout_seconds: float = 25) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(token) < 32
        ):
            raise ValueError(
                "MCP requires a configured endpoint and a service token >=32 characters"
            )
        self.url, self._token, self.timeout = url, token, timeout_seconds

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ToolConnection]:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + self._token, "Accept-Encoding": "identity"},
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
                transport=BoundedHTTPTransport(),
            ) as http,
            Client(
                streamable_http_client(self.url, http_client=http),
                mode=PROTOCOL_VERSION,
                cache=None,
                read_timeout_seconds=self.timeout,
            ) as client,
        ):
            yield MCPConnection(client)
