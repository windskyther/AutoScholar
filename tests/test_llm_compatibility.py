"""Provider compatibility at the actual SDK wire boundary; all HTTP is mocked."""

import copy
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.core.config import Settings
from autoscholar.llm.compatibility import deepseek_schema, resolve_profile
from autoscholar.llm.errors import LLMResponseError, LLMUpstreamError
from autoscholar.llm.models import ChatMessage, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider


def settings(**kwargs: Any) -> Settings:
    return Settings(llm_api_key=SecretStr("offline-only"), llm_model="deepseek-v4-flash", **kwargs)


def bounded_tool() -> ToolDefinition:
    return ToolDefinition(
        name="edit",
        description="Edit one bounded value",
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "string", "minLength": 1, "maxLength": 4},
                "tags": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {"type": "string", "maxLength": 2},
                },
            },
            "required": ["value", "tags"],
            "additionalProperties": False,
        },
    )


def completion(
    arguments: dict[str, Any], *, second: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": "offline",
        "object": "chat.completion",
        "created": 1,
        "model": "offline",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(i),
                            "type": "function",
                            "function": {
                                "name": "edit",
                                "arguments": json.dumps(args),
                            },
                        }
                        for i, args in enumerate([arguments] + ([] if second is None else [second]))
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.deepseek.com", "deepseek"),
        ("https://api.deepseek.com/v1", "deepseek"),
        ("https://api.deepseek.com/beta", "deepseek"),
        ("https://api.deepseek.com.evil.example/v1", "generic"),
        ("https://gateway.example/deepseek", "generic"),
        ("http://api.deepseek.com", "generic"),
    ],
)
def test_auto_profile_uses_exact_official_https_host(url: str, expected: str) -> None:
    assert resolve_profile("auto", url) == expected
    assert resolve_profile("generic", url) == "generic"
    assert resolve_profile("deepseek", url) == "deepseek"


def test_invalid_profile_is_rejected() -> None:
    with pytest.raises(ValidationError):
        settings(llm_compatibility_profile="typo")


@pytest.mark.parametrize("choice", ["auto", "none", "required", "edit"])
@pytest.mark.parametrize("profile", ["generic", "deepseek", "auto"])
async def test_preview_matches_sdk_wire_and_only_forced_calls_disable_thinking(
    choice: str, profile: str
) -> None:
    tool = bounded_tool()
    original = copy.deepcopy(tool.parameters)
    previews = []

    def preview(payload: dict[str, Any], reserve: int) -> None:
        previews.append(copy.deepcopy(payload))

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == previews[-1]
        assert "extra_body" not in body
        active = profile != "generic"
        if active and choice not in {"auto", "none"}:
            assert body["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in body
        schema = body["tools"][0]["function"]["parameters"]
        assert ("maxLength" in schema["properties"]["value"]) is (not active)
        assert body["tool_choice"] == (
            {"type": "function", "function": {"name": "edit"}} if choice == "edit" else choice
        )
        assert body["parallel_tool_calls"] is False
        return httpx.Response(200, json=completion({"value": "ok", "tags": ["x"]}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            settings(llm_base_url="https://api.deepseek.com/v1", llm_compatibility_profile=profile),
            http_client=client,
            request_validator=preview,
        )
        result = await provider.generate(
            [ChatMessage(role="user", content="Edit")], tools=[tool], tool_choice=choice
        )
    assert result.tool_calls[0].arguments == {"value": "ok", "tags": ["x"]}
    assert tool.parameters == original


def test_schema_projection_handles_refs_and_preserves_keyword_named_properties_and_data() -> None:
    original = {
        "type": "object",
        "properties": {
            "maxLength": {"$ref": "#/$defs/value"},
            "list": {
                "type": "array",
                "maxItems": 3,
                "items": {"anyOf": [{"type": "string", "maxLength": 1}, {"type": "null"}]},
            },
        },
        "$defs": {"value": {"type": "string", "minLength": 1}},
        "examples": [{"maxLength": 7}],
        "default": {"minLength": 8},
    }
    before = copy.deepcopy(original)
    projected = deepseek_schema(original)
    assert projected["properties"]["maxLength"] == {"$ref": "#/$defs/value"}
    assert projected["$defs"]["value"] == {"type": "string"}
    assert "maxItems" not in projected["properties"]["list"]
    assert projected["properties"]["list"]["items"]["anyOf"][0] == {"type": "string"}
    assert projected["examples"] == [{"maxLength": 7}]
    assert projected["default"] == {"minLength": 8}
    assert original == before


@pytest.mark.parametrize(
    "bad",
    [
        {"value": "", "tags": ["x"]},
        {"value": "12345", "tags": ["x"]},
        {"value": "ok", "tags": []},
        {"value": "ok", "tags": ["x", "y", "z"]},
        {"value": "ok", "tags": ["xxx"]},
    ],
)
async def test_projected_bounds_still_reject_entire_batch_and_charge_usage(
    bad: dict[str, Any],
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=completion({"value": "ok", "tags": ["x"]}, second=bad))

    budget = Budget(BudgetLimits(model_calls=1))
    token = current_budget.set(budget)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = OpenAICompatibleProvider(
                settings(llm_base_url="https://api.deepseek.com/v1"), http_client=client
            )
            with pytest.raises(LLMResponseError) as caught:
                await BudgetedLLM(provider).generate(
                    [ChatMessage(role="user", content="Edit")],
                    tools=[bounded_tool()],
                    tool_choice="required",
                )
        assert caught.value.validation_reason == "tool_schema_violation"
        assert len(calls) == 1 and budget.used["total_tokens"] == 3
    finally:
        current_budget.reset(token)


async def test_explicit_gateway_profile_does_not_retry_http_400() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "offline rejection"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            settings(
                llm_base_url="https://gateway.example/v1",
                llm_compatibility_profile="deepseek",
            ),
            http_client=client,
        )
        with pytest.raises(LLMUpstreamError):
            await provider.generate(
                [ChatMessage(role="user", content="Edit")],
                tools=[bounded_tool()],
                tool_choice="required",
            )
    assert len(requests) == 1 and requests[0]["thinking"] == {"type": "disabled"}


async def test_external_schema_reference_is_not_fetched() -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=completion({"value": "ok", "tags": ["x"]}))

    tool = bounded_tool()
    tool.parameters["properties"]["value"]["$ref"] = "https://example.org/external-schema"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            settings(llm_base_url="https://api.deepseek.com/v1"), http_client=client
        )
        with pytest.raises(LLMResponseError):
            await provider.generate([ChatMessage(role="user", content="Edit")], tools=[tool])
    assert len(calls) == 1
