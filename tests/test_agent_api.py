from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from autoscholar.agent.records import (
    AgentMode,
    AgentTaskRecord,
    CitationRecord,
    EvidenceRecord,
    ResearchWarningRecord,
    ToolTraceRecord,
)
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


def research_task() -> AgentTaskRecord:
    now = datetime.now(UTC)
    evidence = EvidenceRecord(
        id="evidence-1",
        task_id="task-research",
        citation_key="E1",
        source_type="paper",
        provider="semantic_scholar",
        title="LoRA",
        url="https://doi.org/10.1/lora",
        authors=("Alice",),
        year=2021,
        external_id="10.1/lora",
        query="LoRA paper",
        topic="LoRA",
        claim="LoRA uses low-rank adaptation",
        excerpt="We propose low-rank adaptation.",
        relevance=0.95,
        created_at=now,
    )
    return replace(
        completed_task(),
        id="task-research",
        mode="research",
        answer="LoRA uses low-rank adaptation [E1].",
        citations=[CitationRecord(claim="LoRA uses low-rank adaptation", evidence_ids=("E1",))],
        warnings=[
            ResearchWarningRecord(
                code="web_search_partial",
                message="One web query failed",
                provider="tavily",
            )
        ],
        evidence=[evidence],
    )


class FakeAgentBackend:
    def __init__(self, task: AgentTaskRecord | None) -> None:
        self.task = task
        self.objective: str | None = None
        self.mode: AgentMode | None = None

    async def run(
        self,
        objective: str,
        *,
        task_id: str | None = None,
        mode: AgentMode = "auto",
    ) -> AgentRunResult:
        del task_id
        self.objective = objective
        self.mode = mode
        assert self.task is not None
        return AgentRunResult(task=self.task, model="test-model")

    async def create_task(
        self, *, task_id: str, objective: str, project_id: str | None = None
    ) -> AgentTaskRecord:
        del task_id, objective, project_id
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

    async def list_evidence(
        self, task_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[EvidenceRecord], int]:
        del task_id
        items = self.task.evidence if self.task else []
        return items[offset : offset + limit], len(items)

    async def add_evidence(
        self,
        *,
        task_id: str,
        citation_key: str,
        source_type: str,
        provider: str,
        title: str,
        url: str,
        authors: tuple[str, ...],
        year: int | None,
        external_id: str | None,
        query: str,
        topic: str,
        claim: str,
        excerpt: str,
        relevance: float,
        document_id: str | None = None,
        chunk_id: str | None = None,
        page: int | None = None,
        section: str | None = None,
    ) -> EvidenceRecord:
        del (
            task_id,
            citation_key,
            source_type,
            provider,
            title,
            url,
            authors,
            year,
            external_id,
            query,
            topic,
            claim,
            excerpt,
            relevance,
            document_id,
            chunk_id,
            page,
            section,
        )
        raise NotImplementedError


class FailingAgentBackend(FakeAgentBackend):
    async def run(
        self,
        objective: str,
        *,
        task_id: str | None = None,
        mode: AgentMode = "auto",
    ) -> AgentRunResult:
        del objective, task_id, mode
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
    assert payload["mode"] == "compute"
    assert payload["evidence"] == []
    assert payload["citations"] == []
    assert payload["warnings"] == []
    assert backend.objective == "Calculate 2+2"


def test_post_agent_run_passes_explicit_mode_to_runner() -> None:
    backend = FakeAgentBackend(completed_task())
    with client_with_backend(backend) as client:
        response = client.post(
            "/agent/run",
            json={"objective": "Research LoRA", "mode": "research"},
        )

    assert response.status_code == 200
    assert backend.mode == "research"


def test_get_agent_task_returns_persisted_record() -> None:
    with client_with_backend(FakeAgentBackend(completed_task())) as client:
        response = client.get("/agent/tasks/task-1")

    assert response.status_code == 200
    assert response.json()["objective"] == "Calculate 2+2"
    assert response.json()["tool_calls"][0]["call_id"] == "call-1"


def test_get_agent_evidence_returns_paginated_records() -> None:
    with client_with_backend(FakeAgentBackend(research_task())) as client:
        response = client.get("/agent/tasks/task-research/evidence?limit=1&offset=0")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["citation_key"] == "E1"
    assert response.json()["items"][0]["authors"] == ["Alice"]


def test_get_agent_evidence_requires_existing_task() -> None:
    with client_with_backend(FakeAgentBackend(None)) as client:
        response = client.get("/agent/tasks/missing/evidence")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_task_not_found"


def test_get_agent_task_returns_stable_404() -> None:
    with client_with_backend(FakeAgentBackend(None)) as client:
        response = client.get("/agent/tasks/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "agent_task_not_found"


def test_post_agent_run_validates_objective() -> None:
    with client_with_backend(FakeAgentBackend(completed_task())) as client:
        response = client.post("/agent/run", json={"objective": "   "})

    assert response.status_code == 422


def test_post_agent_run_rejects_unknown_mode() -> None:
    with client_with_backend(FakeAgentBackend(completed_task())) as client:
        response = client.post(
            "/agent/run",
            json={"objective": "Research LoRA", "mode": "unknown"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


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
