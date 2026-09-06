from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.agent.runner import AgentLimits, AgentRunner
from autoscholar.agent.tools import RestrictedPythonTool
from autoscholar.llm import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)


class ScriptedProvider:
    configured = True

    def __init__(self, responses: list[LLMResult]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


def response(*, text: str = "", tool_call: ToolCall | None = None) -> LLMResult:
    return LLMResult(
        text=text,
        model="test-model",
        usage=TokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        tool_calls=(tool_call,) if tool_call else (),
    )


async def repository() -> tuple[AgentTaskRepository, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False)), engine


async def test_langgraph_agent_plans_executes_tool_and_writes_answer() -> None:
    store, engine = await repository()
    provider = ScriptedProvider(
        [
            response(
                tool_call=ToolCall(
                    id="plan-1",
                    name="submit_plan",
                    arguments={"steps": ["Calculate values", "Summarize properties"]},
                )
            ),
            response(
                tool_call=ToolCall(
                    id="python-1",
                    name="python",
                    arguments={
                        "code": (
                            "values = [x * x for x in range(11)]\n"
                            "print(min(values), max(values))"
                        )
                    },
                )
            ),
            response(text="The computation found endpoints 0 and 100."),
            response(text="y=x² is increasing on [0,10], with min (0,0) and max (10,100)."),
        ]
    )
    runner = AgentRunner(
        provider=provider,
        repository=store,
        tools=[RestrictedPythonTool()],
    )

    result = await runner.run("Analyze y=x² on [0,10]", task_id="task-graph")

    assert result.task.status == "succeeded"
    assert result.task.metrics == {
        "iterations": 2,
        "model_calls": 4,
        "input_tokens": 8,
        "output_tokens": 4,
        "total_tokens": 12,
    }
    assert result.task.tool_calls[0].tool_name == "python"
    assert result.task.tool_calls[0].output == "0 100"
    assert "increasing" in (result.task.answer or "")
    assert provider.calls[0]["tool_choice"] == "auto"
    await engine.dispose()


async def test_agent_writes_partial_answer_when_tool_budget_is_exceeded() -> None:
    store, engine = await repository()
    provider = ScriptedProvider(
        [
            response(
                tool_call=ToolCall(
                    id="plan-1",
                    name="submit_plan",
                    arguments={"steps": ["Calculate"]},
                )
            ),
            response(
                tool_call=ToolCall(
                    id="python-1",
                    name="python",
                    arguments={"code": "print(4)"},
                )
            ),
            response(text="Partial answer: no tool execution budget was available."),
        ]
    )
    runner = AgentRunner(
        provider=provider,
        repository=store,
        tools=[RestrictedPythonTool()],
        limits=AgentLimits(max_tool_calls=0),
    )

    result = await runner.run("Calculate 2+2", task_id="task-budget")

    assert result.task.status == "budget_exceeded"
    assert result.task.answer is not None
    assert result.task.tool_calls == []
    await engine.dispose()
