"""Research contract shared by the independent server and Core's read-only adapter."""

from typing import Any, Literal

from pydantic import TypeAdapter

from autoscholar.research.models import SearchResponse, SourceType
from autoscholar.research.providers import SearchProviderError
from autoscholar.tool_platform.documents import DOCUMENT_INPUT
from autoscholar.tool_platform.gateway import ToolContract, ToolGateway, ToolGatewayError, canonical

SEARCH_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 400},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["query", "limit"],
    "additionalProperties": False,
}
SEARCH_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "response": {"type": ["object", "null"]},
        "error_code": {"type": ["string", "null"]},
        "uncertain": {"type": "boolean"},
    },
    "required": ["ok", "response", "error_code", "uncertain"],
    "additionalProperties": False,
}
SEARCH_CONTRACTS = [
    ToolContract(name, SEARCH_INPUT, SEARCH_OUTPUT) for name in ("search_web", "search_papers")
]
DOCUMENT_CONTRACT = ToolContract("get_document", DOCUMENT_INPUT, SEARCH_OUTPUT)
RESEARCH_CONTRACTS = [*SEARCH_CONTRACTS, DOCUMENT_CONTRACT]


class MCPResearchSearch:
    """Runner consumes search/tool budget; the gateway journals the remote operation once."""

    def __init__(self, gateway: ToolGateway, source_type: Literal["web", "paper"]) -> None:
        self.gateway = gateway
        self.source_type: SourceType = source_type
        self.name = "tavily" if source_type == "web" else "semantic_scholar"
        self.tool = "search_web" if source_type == "web" else "search_papers"

    @property
    def configured(self) -> bool:
        # This means a configured MCP route, not proof of upstream availability.
        return True

    async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        query = " ".join(query.split())
        try:
            envelope = await self.gateway.invoke(self.tool, {"query": query, "limit": limit})
        except ToolGatewayError as exc:
            raise SearchProviderError(code=exc.code, message="Research MCP request failed") from exc
        if not envelope["ok"]:
            raise SearchProviderError(
                code="mcp_search_not_configured", message="Research MCP provider is not configured"
            )
        try:
            response = TypeAdapter(SearchResponse).validate_json(canonical(envelope["response"]))
            if (
                response.provider != self.name
                or response.source_type != self.source_type
                or response.query != query
                or len(response.results) > limit
                or any(
                    item.provider != self.name or item.source_type != self.source_type
                    for item in response.results
                )
            ):
                raise ValueError("Unexpected research provenance")
            return response
        except (ValueError, TypeError) as exc:
            raise SearchProviderError(
                code="tool_result_invalid", message="Research MCP returned an invalid result"
            ) from exc

    async def close(self) -> None:
        # Connections are bounded to one invocation; no cross-task async exit stacks.
        return None
