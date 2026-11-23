from autoscholar.core.config import Settings
from autoscholar.llm.base import LLMProvider
from autoscholar.llm.errors import LLMNotConfiguredError
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider


class UnconfiguredLLMProvider:
    @property
    def configured(self) -> bool:
        return False

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        del messages, tools, tool_choice
        raise LLMNotConfiguredError

    async def close(self) -> None:
        return None


def create_llm_provider(settings: Settings) -> LLMProvider:
    if not settings.llm_configured:
        return UnconfiguredLLMProvider()
    return OpenAICompatibleProvider(settings)
