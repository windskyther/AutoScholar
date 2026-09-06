import json

import httpx
import pytest
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.llm.errors import LLMNotConfiguredError, LLMUnavailableError
from autoscholar.llm.factory import create_llm_provider
from autoscholar.llm.models import (
    AssistantToolCallMessage,
    ChatMessage,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
)
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
async def test_provider_round_trips_native_tool_calls() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tool_choice"] == "required"
        assert payload["parallel_tool_calls"] is False
        assert payload["tools"][0]["function"]["name"] == "calculator"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-tool",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": '{"expression":"2+2"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )

    tool = ToolDefinition(
        name="calculator",
        description="Evaluate arithmetic",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenAICompatibleProvider(configured_settings(), http_client=http_client)
        result = await provider.generate(
            [ChatMessage(role="user", content="calculate 2+2")],
            tools=[tool],
            tool_choice="required",
        )

    assert result.text == ""
    assert result.tool_calls == (
        ToolCall(id="call-1", name="calculator", arguments={"expression": "2+2"}),
    )


@pytest.mark.asyncio
async def test_provider_serializes_tool_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["messages"][1]["tool_calls"][0]["id"] == "call-1"
        assert payload["messages"][2] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "4",
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-after-tool",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "The answer is 4."},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    messages: list[ChatMessage | AssistantToolCallMessage | ToolResultMessage] = [
        ChatMessage(role="user", content="calculate 2+2"),
        AssistantToolCallMessage(
            tool_calls=(
                ToolCall(id="call-1", name="calculator", arguments={"expression": "2+2"}),
            )
        ),
        ToolResultMessage(tool_call_id="call-1", content="4"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        provider = OpenAICompatibleProvider(configured_settings(), http_client=http_client)
        result = await provider.generate(messages)

    assert result.text == "The answer is 4."


@pytest.mark.asyncio
async def test_unconfigured_provider_fails_safely() -> None:
    provider = create_llm_provider(Settings(llm_api_key=None, llm_model=None))

    assert provider.configured is False
    with pytest.raises(LLMNotConfiguredError):
        await provider.generate([ChatMessage(role="user", content="hello")])
