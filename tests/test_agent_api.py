from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from autoscholar.agent.records import AgentTaskRecord, ToolTraceRecord
from autoscholar.agent.runner import AgentRunError, AgentRunResult
from autoscholar.core.config import Settings
from autoscholar.main import create_app
from tests.test_chat import SuccessfulProvider
from tests.test_health import FakeDependency


def completed_task() -> AgentTaskRecord:
    now = datetime.now(UTC)
    return AgentTaskRecord(
        id="task-1",
        status="succeeded",
        objective="Calculate 2+2",
        plan=["Calculate"],
        answer="4",
        metrics={
            "iterations": 2,
            "model_calls": 4,
            "input_tokens": 8,
            "output_tokens": 4,
            "total_tokens": 12,
        },
        created_at=now,
        updated_at=now,
        tool_calls=[
            ToolTraceRecord(
                id="trace-1",
                task_id="task-1",
                sequence=1,
                call_id="call-1",
                tool_name="calculator",
                arguments={"expression": "2+2"},
                output="4",
                status="succeeded",
                duration_ms=0.5,
                created_at=now,
            )
        ],
    )


class FakeAgentBackend:
    def __init__(self, task: AgentTaskRecord | None) -> None:
        self.task = task
        self.objective: str | None = None

    async def run(self, objective: str, *, task_id: str | None = None) -> AgentRunResult:
        del task_id
        self.objective = objective
        assert self.task is not None
        return AgentRunResult(task=self.task, model="test-model")

    async def create_task(self, *, task_id: str, objective: str) -> AgentTaskRecord:
        del task_id, objective
        raise NotImplementedError

    async def update_task(self, *args: object, **kwargs: object) -> AgentTaskRecord:
        del args, kwargs
        raise NotImplementedError

    async def add_tool_call(self, *args: object, **kwargs: object) -> ToolTraceRecord:
        del args, kwargs
        raise NotImplementedError

    async def get_task(self, task_id: str) -> AgentTaskRecord | None:
        del task_id
        return self.task


class FailingAgentBackend(FakeAgentBackend):
    async def run(self, objective: str, *, task_id: str | None = None) -> AgentRunResult:
        del objective, task_id
        raise AgentRunError(
            task_id="failed-task",
            code="native_tool_calling_required",
            message="The configured model did not return the required native tool call",
        )


def client_with_backend(backend: FakeAgentBackend) -> TestClient:
    return TestClient(
        create_app(
            Settings(llm_api_key=None, llm_model=None),
            database=FakeDependency(),
            redis=FakeDependency(),
            llm_provider=SuccessfulProvider(),
            agent_repository=backend,
            agent_runner=backend,
        )
    )


def test_post_agent_run_returns_plan_answer_trace_and_metrics() -> None:
    backend = FakeAgentBackend(completed_task())
    with client_with_backend(backend) as client:
        response = client.post(
            "/agent/run",
            json={"objective": "  Calculate 2+2  "},
            headers={"X-Request-ID": "agent-request"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "task-1"
    assert payload["status"] == "succeeded"
    assert payload["answer"] == "4"
    assert payload["tool_calls"][0]["tool_name"] == "calculator"
    assert payload["metrics"]["model_calls"] == 4
    assert payload["request_id"] == "agent-request"
    assert backend.objective == "Calculate 2+2"


def test_get_agent_task_returns_persisted_record() -> None:
    with client_with_backend(FakeAgentBackend(completed_task())) as client:
        response = client.get("/agent/tasks/task-1")

    assert response.status_code == 200
    assert response.json()["objective"] == "Calculate 2+2"
    assert response.json()["tool_calls"][0]["call_id"] == "call-1"


def test_get_agent_task_returns_stable_404() -> None:
    with client_with_backend(FakeAgentBackend(None)) as client:
        response = client.get("/agent/tasks/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_task_not_found"


def test_post_agent_run_validates_objective() -> None:
    with client_with_backend(FakeAgentBackend(completed_task())) as client:
        response = client.post("/agent/run", json={"objective": "   "})

    assert response.status_code == 422


def test_agent_error_response_carries_persisted_task_id() -> None:
    with client_with_backend(FailingAgentBackend(None)) as client:
        response = client.post("/agent/run", json={"objective": "Calculate 2+2"})

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "native_tool_calling_required",
        "message": "The configured model did not return the required native tool call",
        "task_id": "failed-task",
    }


def test_agent_response_explicitly_declares_utf8_and_preserves_chinese() -> None:
    task = replace(
        completed_task(),
        objective="分析函数",
        plan=["求导数"],
        answer="函数严格递增",
    )
    with client_with_backend(FakeAgentBackend(task)) as client:
        response = client.post("/agent/run", json={"objective": "分析函数"})

    assert response.headers["content-type"] == "application/json; charset=utf-8"
    decoded = response.content.decode("utf-8")
    assert "求导数" in decoded
    assert "函数严格递增" in decoded
