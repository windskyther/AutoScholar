from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.agent.runner import AgentRunner
from autoscholar.coding.agent import CodingAgent, CodingLimits, CodingRunError
from autoscholar.coding.errors import ErrorParser
from autoscholar.coding.sandbox import (
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.llm import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.rag import database_models as rag_database_models  # noqa: F401


class ScriptedCodingProvider:
    configured = True

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = calls
        self.prompts: list[list[ConversationMessage]] = []

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        del tools, tool_choice
        self.prompts.append(messages)
        return LLMResult(
            text="",
            model="coding-model",
            usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            tool_calls=(self.calls.pop(0),),
        )

    async def close(self) -> None:
        return None


class FakeSandbox:
    def __init__(self, results: list[SandboxRunResult]) -> None:
        self.results = results
        self.requests: list[SandboxRunRequest] = []

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.requests.append(request)
        return self.results.pop(0)

    async def health(self) -> SandboxHealth:
        return SandboxHealth(status="ok", engine=True, image=True, mnist_dataset=True)

    async def close(self) -> None:
        return None


def call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def run_result(
    status: Literal["succeeded", "failed", "timed_out"] = "succeeded",
    *,
    stderr: str = "",
    exit_code: int | None = 0,
) -> SandboxRunResult:
    return SandboxRunResult(
        status=status,
        exit_code=exit_code,
        stdout="ok\n" if status == "succeeded" else "",
        stderr=stderr,
        duration_ms=10,
    )


async def repository() -> tuple[AgentTaskRepository, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False)), engine


async def test_coding_agent_creates_files_and_requires_static_and_pytest(
    tmp_path: Path,
) -> None:
    store, engine = await repository()
    await store.create_task(task_id="coding-1", objective="Build a module")
    provider = ScriptedCodingProvider(
        [
            call(
                "create-1",
                "create_file",
                {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"},
            ),
            call(
                "create-2",
                "create_file",
                {
                    "path": "test_app.py",
                    "content": (
                        "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
                    ),
                },
            ),
            call("ready-1", "submit_code_ready", {"summary": "implementation complete"}),
        ]
    )
    sandbox = FakeSandbox([run_result(), run_result()])
    workspaces = WorkspaceManager(tmp_path)
    agent = CodingAgent(
        provider=provider,
        repository=store,
        workspaces=workspaces,
        sandbox=sandbox,
    )

    result = await agent.run(
        task_id="coding-1", objective="Build a tested add function", plan=["Implement", "Test"]
    )

    assert "completed successfully" in result.answer
    assert result.model_calls == 3
    assert result.files_written == 2
    assert result.sandbox_runs == 2
    assert [request.action for request in sandbox.requests] == ["static_check", "run_pytest"]
    assert sandbox.requests[0].files["app.py"].startswith("def add")
    persisted = await store.get_task("coding-1")
    assert persisted is not None
    assert [trace.tool_name for trace in persisted.tool_calls] == [
        "create_file",
        "create_file",
        "static_check",
        "run_pytest",
    ]
    assert workspaces.read_text("coding-1", "test_app.py").startswith("from app")
    await engine.dispose()


async def test_coding_agent_parses_failure_and_repairs_before_retry(tmp_path: Path) -> None:
    store, engine = await repository()
    await store.create_task(task_id="coding-repair", objective="Repair code")
    provider = ScriptedCodingProvider(
        [
            call(
                "create-1",
                "create_file",
                {"path": "app.py", "content": "def value(:\n    return 1\n"},
            ),
            call(
                "create-2",
                "create_file",
                {
                    "path": "test_app.py",
                    "content": "def test_value():\n    assert True\n",
                },
            ),
            call("ready-1", "submit_code_ready", {"summary": "ready"}),
            call("read-1", "read_file", {"path": "app.py"}),
            call(
                "edit-1",
                "edit_file",
                {
                    "path": "app.py",
                    "old_text": "def value(:",
                    "new_text": "def value():",
                },
            ),
            call("ready-2", "submit_code_ready", {"summary": "repaired"}),
        ]
    )
    sandbox = FakeSandbox(
        [
            run_result("failed", stderr="SyntaxError: invalid syntax", exit_code=1),
            run_result(),
            run_result(),
        ]
    )
    agent = CodingAgent(
        provider=provider,
        repository=store,
        workspaces=WorkspaceManager(tmp_path),
        sandbox=sandbox,
    )

    result = await agent.run(
        task_id="coding-repair", objective="Create valid code", plan=["Create", "Repair"]
    )

    assert result.repair_attempts == 1
    assert result.sandbox_runs == 3
    assert any("syntax_error" in str(message) for message in provider.prompts[3])
    await engine.dispose()


async def test_coding_agent_stops_when_repair_budget_is_exhausted(tmp_path: Path) -> None:
    store, engine = await repository()
    await store.create_task(task_id="coding-fail", objective="Fail")
    provider = ScriptedCodingProvider(
        [
            call("create-1", "create_file", {"path": "broken.py", "content": "broken("}),
            call("ready-1", "submit_code_ready", {"summary": "ready"}),
        ]
    )
    agent = CodingAgent(
        provider=provider,
        repository=store,
        workspaces=WorkspaceManager(tmp_path),
        sandbox=FakeSandbox(
            [run_result("failed", stderr="SyntaxError: invalid syntax", exit_code=1)]
        ),
        limits=CodingLimits(max_repairs=0),
    )

    with pytest.raises(CodingRunError) as error:
        await agent.run(task_id="coding-fail", objective="Fail", plan=["Create"])
    assert error.value.code == "code_repair_exhausted"
    await engine.dispose()


async def test_main_agent_routes_coding_mode_and_persists_metrics(tmp_path: Path) -> None:
    store, engine = await repository()
    provider = ScriptedCodingProvider(
        [
            call(
                "plan-1",
                "submit_plan",
                {"mode": "coding", "steps": ["Create module", "Run tests"]},
            ),
            call(
                "create-1",
                "create_file",
                {"path": "main.py", "content": "def ok():\n    return True\n"},
            ),
            call(
                "create-2",
                "create_file",
                {"path": "test_main.py", "content": "def test_ok():\n    assert True\n"},
            ),
            call("ready-1", "submit_code_ready", {"summary": "ready"}),
        ]
    )
    sandbox = FakeSandbox([run_result(), run_result()])
    coding = CodingAgent(
        provider=provider,
        repository=store,
        workspaces=WorkspaceManager(tmp_path),
        sandbox=sandbox,
    )
    runner = AgentRunner(
        provider=provider,
        repository=store,
        tools=[],
        coding_service=coding,
    )

    result = await runner.run(
        "Create a tested module", task_id="routed-coding", mode="coding"
    )

    assert result.task.status == "succeeded"
    assert result.task.mode == "coding"
    assert result.task.metrics["sandbox_runs"] == 2
    assert result.task.metrics["files_written"] == 2
    assert result.task.metrics["model_calls"] == 4
    await engine.dispose()


def test_error_parser_classifies_and_redacts_diagnostics() -> None:
    diagnostic = ErrorParser.parse(
        run_result(
            "failed",
            stderr="CUDA out of memory; API_KEY=secret-value; token: abc123",
            exit_code=1,
        )
    )

    assert diagnostic.category == "cuda_oom"
    assert "secret-value" not in diagnostic.stderr
    assert "abc123" not in diagnostic.stderr
