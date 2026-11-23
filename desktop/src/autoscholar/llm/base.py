from typing import Protocol

from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition


class LLMProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult: ...

    async def close(self) -> None: ...
