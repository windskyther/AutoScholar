from dataclasses import dataclass
from typing import Literal

ChatRole = Literal["developer", "system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: ChatRole
    content: str


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    usage: TokenUsage | None = None
