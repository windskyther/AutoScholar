import json

import httpx
import pytest
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.llm.errors import LLMNotConfiguredError, LLMUnavailableError
from autoscholar.llm.factory import create_llm_provider
from autoscholar.llm.models import ChatMessage
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider


def configured_settings() -> Settings:
    return Settings(
        llm_base_url="https://llm.example/v1",
        llm_api_key=SecretStr("test-secret"),
        llm_model="test-model",
    )


@pytest.mark.asyncio
async def test_openai_compatible_provider_returns_text_and_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "world"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenAICompatibleProvider(configured_settings(), http_client=http_client)
        result = await provider.generate([ChatMessage(role="user", content="hello")])

    assert result.text == "world"
    assert result.model == "test-model"
    assert result.usage is not None
    assert result.usage.total_tokens == 3


@pytest.mark.asyncio
async def test_provider_maps_connection_failures() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenAICompatibleProvider(configured_settings(), http_client=http_client)
        with pytest.raises(LLMUnavailableError) as exc_info:
            await provider.generate([ChatMessage(role="user", content="hello")])

    assert exc_info.value.code == "llm_unavailable"
    assert "offline" not in exc_info.value.message


@pytest.mark.asyncio
async def test_unconfigured_provider_fails_safely() -> None:
    provider = create_llm_provider(Settings())

    assert provider.configured is False
    with pytest.raises(LLMNotConfiguredError):
        await provider.generate([ChatMessage(role="user", content="hello")])
