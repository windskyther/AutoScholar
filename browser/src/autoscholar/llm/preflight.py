"""Optional, side-effect-free request admission for Providers and their wrappers."""

from autoscholar.llm.base import LLMProvider
from autoscholar.llm.models import ConversationMessage, ToolChoice, ToolDefinition


async def preflight_request(
    provider: LLMProvider,
    messages: list[ConversationMessage],
    *,
    tools: list[ToolDefinition] | None = None,
    tool_choice: ToolChoice = "none",
    reserve_bytes: int = 0,
) -> None:
    """Preview only: no calls, counters, journals, files, grants or network effects."""
    check = getattr(provider, "preflight", None)
    if callable(check):
        await check(messages, tools=tools, tool_choice=tool_choice, reserve_bytes=reserve_bytes)
