from fastapi.testclient import TestClient

from autoscholar.coding.sandbox import SandboxHealth, SandboxRunRequest, SandboxRunResult
from autoscholar.core.config import Settings
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.main import create_app


class FakeDependency:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.closed = False

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("dependency unavailable")
        return True

    async def close(self) -> None:
        self.closed = True


class FakeLLMProvider:
    configured = True

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        del messages, tools, tool_choice
        return LLMResult(text="ok", model="test-model")

    async def close(self) -> None:
        return None


class FakeSandbox:
    def __init__(self, *, healthy: bool = True, mnist: bool = True) -> None:
        self.healthy = healthy
        self.mnist = mnist

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        del request
        raise NotImplementedError

    async def health(self) -> SandboxHealth:
        return SandboxHealth(
            status="ok" if self.healthy else "error",
            engine=self.healthy,
            image=self.healthy,
            mnist_dataset=self.mnist,
        )

    async def close(self) -> None:
        return None


def create_test_client(
    *,
    database_healthy: bool = True,
    redis_healthy: bool = True,
    qdrant_healthy: bool = True,
) -> TestClient:
    return TestClient(
        create_app(
            Settings(),
            database=FakeDependency(healthy=database_healthy),
            redis=FakeDependency(healthy=redis_healthy),
            qdrant=FakeDependency(healthy=qdrant_healthy),
            llm_provider=FakeLLMProvider(),
            sandbox_executor=FakeSandbox(),
        )
    )


def test_liveness_and_request_id() -> None:
    client = create_test_client()

    response = client.get("/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request"
    assert response.json() == {
        "status": "ok",
        "service": "autoscholar",
        "version": "0.1.0",
    }


def test_invalid_request_id_is_replaced() -> None:
    client = create_test_client()

    response = client.get("/health/live", headers={"X-Request-ID": "invalid request id"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "invalid request id"


def test_readiness_when_dependencies_are_healthy() -> None:
    with create_test_client() as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {
            "postgres": {"status": "ok"},
            "redis": {"status": "ok"},
            "qdrant": {"status": "ok"},
        },
        "capabilities": {
            "llm": {"status": "ok"},
            "web_search": {"status": "not_configured"},
            "paper_search": {"status": "ok"},
            "rag": {"status": "ok"},
            "sandbox": {"status": "ok"},
            "mnist_dataset": {"status": "ok"},
        },
    }


def test_readiness_when_a_dependency_is_unavailable() -> None:
    with create_test_client(redis_healthy=False) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {
            "postgres": {"status": "ok"},
            "redis": {"status": "error"},
            "qdrant": {"status": "ok"},
        },
        "capabilities": {
            "llm": {"status": "ok"},
            "web_search": {"status": "not_configured"},
            "paper_search": {"status": "ok"},
            "rag": {"status": "ok"},
            "sandbox": {"status": "ok"},
            "mnist_dataset": {"status": "ok"},
        },
    }


def test_readiness_reports_unconfigured_llm_without_failing_infrastructure() -> None:
    app = create_app(
        Settings(llm_api_key=None, llm_model=None),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        sandbox_executor=FakeSandbox(),
    )

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["capabilities"]["llm"] == {"status": "not_configured"}
