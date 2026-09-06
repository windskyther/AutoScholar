from typing import cast

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam

from autoscholar.core.config import Settings
from autoscholar.llm.errors import LLMUnavailableError, LLMUpstreamError
from autoscholar.llm.models import ChatMessage, LLMResult, TokenUsage


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

    async def generate(self, messages: list[ChatMessage]) -> LLMResult:
        request_messages = [
            cast(ChatCompletionMessageParam, {"role": message.role, "content": message.content})
            for message in messages
        ]
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=request_messages,
            )
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

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LLMUpstreamError(
                code="llm_empty_response",
                message="The language model provider returned no text",
            )

        usage = None
        if response.usage is not None:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )

        return LLMResult(text=content, model=response.model or self._model, usage=usage)

    async def close(self) -> None:
        await self._client.close()

