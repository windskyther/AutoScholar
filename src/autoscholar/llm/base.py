from typing import Protocol

from autoscholar.llm.models import ChatMessage, LLMResult


class LLMProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def generate(self, messages: list[ChatMessage]) -> LLMResult: ...

    async def close(self) -> None: ...

