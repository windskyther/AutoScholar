from dataclasses import dataclass
from typing import Any, Literal

ChatRole = Literal["developer", "system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: ChatRole
    content: str


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AssistantToolCallMessage:
    tool_calls: tuple[ToolCall, ...]
    content: str = ""
    reasoning_content: str | None = None
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    tool_call_id: str
    content: str
    role: Literal["tool"] = "tool"


type ConversationMessage = ChatMessage | AssistantToolCallMessage | ToolResultMessage
type ToolChoice = Literal["none", "auto", "required"] | str


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
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None
