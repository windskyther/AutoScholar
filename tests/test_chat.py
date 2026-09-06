import pytest
from fastapi.testclient import TestClient

from autoscholar.core.config import Settings
from autoscholar.llm.errors import LLMUnavailableError
from autoscholar.llm.factory import UnconfiguredLLMProvider
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.main import create_app
from tests.test_health import FakeDependency


class SuccessfulProvider:
    configured = True

    def __init__(self) -> None:
        self.messages: list[ConversationMessage] = []

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        del tools, tool_choice
        self.messages = messages
        return LLMResult(
            text="The answer",
            model="test-model",
            usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
        )

    async def close(self) -> None:
        return None


class FailingProvider:
    configured = True

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        del messages, tools, tool_choice
        raise LLMUnavailableError(code="llm_timeout", message="Provider timed out")

    async def close(self) -> None:
        return None


def client_with_provider(
    provider: SuccessfulProvider | FailingProvider | UnconfiguredLLMProvider,
) -> TestClient:
    return TestClient(
        create_app(
            Settings(),
            database=FakeDependency(),
            redis=FakeDependency(),
            llm_provider=provider,
        )
    )


def test_chat_returns_provider_result_and_usage() -> None:
    provider = SuccessfulProvider()

    with client_with_provider(provider) as client:
        response = client.post(
            "/chat",
            json={"message": "  hello  "},
            headers={"X-Request-ID": "chat-test"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "The answer",
        "model": "test-model",
        "request_id": "chat-test",
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }
    assert provider.messages == [ChatMessage(role="user", content="hello")]


@pytest.mark.parametrize("message", ["", "   "])
def test_chat_rejects_empty_messages(message: str) -> None:
    with client_with_provider(SuccessfulProvider()) as client:
        response = client.post("/chat", json={"message": message})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_chat_rejects_oversized_messages() -> None:
    with client_with_provider(SuccessfulProvider()) as client:
        response = client.post("/chat", json={"message": "x" * 10_001})

    assert response.status_code == 422


def test_chat_returns_503_when_llm_is_not_configured() -> None:
    with client_with_provider(UnconfiguredLLMProvider()) as client:
        response = client.post("/chat", json={"message": "hello"})

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "llm_not_configured",
        "message": "The language model provider is not configured",
    }


def test_chat_maps_provider_unavailability() -> None:
    with client_with_provider(FailingProvider()) as client:
        response = client.post("/chat", json={"message": "hello"})

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "llm_timeout",
        "message": "Provider timed out",
    }
