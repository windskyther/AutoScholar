import json
from typing import Any, cast

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from autoscholar.core.config import Settings
from autoscholar.llm.errors import LLMUnavailableError, LLMUpstreamError
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResultMessage,
)


class OpenAICompatibleProvider:
    """OpenAI-compatible provider backed by the async official SDK."""

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        if not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM API key and model are required")

        self._model = settings.llm_model
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    @property
    def configured(self) -> bool:
        return True

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        request_messages = [self._serialize_message(message) for message in messages]
        request_tools = [self._serialize_tool(tool) for tool in tools] if tools else None
        request_tool_choice: Any = tool_choice
        if tool_choice not in {"none", "auto", "required"}:
            request_tool_choice = {"type": "function", "function": {"name": tool_choice}}
        request: dict[str, Any] = {"model": self._model, "messages": request_messages}
        if request_tools:
            request.update(
                tools=request_tools,
                tool_choice=request_tool_choice,
                parallel_tool_calls=False,
            )
        try:
            response = await self._client.chat.completions.create(**request)
        except AuthenticationError as exc:
            raise LLMUpstreamError(
                code="llm_authentication_failed",
                message="The language model provider rejected its credentials",
            ) from exc
        except RateLimitError as exc:
            raise LLMUnavailableError(
                code="llm_rate_limited",
                message="The language model provider is rate limited",
            ) from exc
        except APITimeoutError as exc:
            raise LLMUnavailableError(
                code="llm_timeout",
                message="The language model provider timed out",
            ) from exc
        except APIConnectionError as exc:
            raise LLMUnavailableError(
                code="llm_unavailable",
                message="The language model provider is unavailable",
            ) from exc
        except APIStatusError as exc:
            raise LLMUpstreamError(message="The language model provider returned an error") from exc

        message = response.choices[0].message if response.choices else None
        content = message.content if message is not None else None
        raw_message: Any = message
        reasoning_content = (
            getattr(raw_message, "reasoning_content", None) if message is not None else None
        )
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            reasoning_content = None
        tool_calls: list[ToolCall] = []
        if message is not None and message.tool_calls:
            for typed_call in message.tool_calls:
                raw_call: Any = typed_call
                try:
                    arguments = json.loads(raw_call.function.arguments)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise LLMUpstreamError(
                        code="llm_invalid_tool_call",
                        message="The language model returned invalid tool arguments",
                    ) from exc
                if not isinstance(arguments, dict):
                    raise LLMUpstreamError(
                        code="llm_invalid_tool_call",
                        message="The language model returned invalid tool arguments",
                    )
                tool_calls.append(
                    ToolCall(
                        id=raw_call.id,
                        name=raw_call.function.name,
                        arguments=arguments,
                    )
                )

        if not content and not tool_calls:
            raise LLMUpstreamError(
                code="llm_empty_response",
                message="The language model provider returned no text or tool calls",
            )

        usage = None
        if response.usage is not None:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )

        return LLMResult(
            text=content or "",
            model=response.model or self._model,
            usage=usage,
            tool_calls=tuple(tool_calls),
            reasoning_content=reasoning_content,
        )

    @staticmethod
    def _serialize_message(message: ConversationMessage) -> ChatCompletionMessageParam:
        if isinstance(message, ChatMessage):
            return cast(
                ChatCompletionMessageParam,
                {"role": message.role, "content": message.content},
            )
        if isinstance(message, ToolResultMessage):
            return cast(
                ChatCompletionMessageParam,
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                },
            )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ],
        }
        if message.reasoning_content is not None:
            assistant_message["reasoning_content"] = message.reasoning_content
        return cast(ChatCompletionMessageParam, assistant_message)

    @staticmethod
    def _serialize_tool(tool: ToolDefinition) -> ChatCompletionToolParam:
        return cast(
            ChatCompletionToolParam,
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": tool.strict,
                },
            },
        )

    async def close(self) -> None:
        await self._client.close()
