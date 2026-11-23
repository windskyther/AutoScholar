import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.core.budget import (
    Budget,
    BudgetedLLM,
    BudgetExceeded,
    BudgetLimits,
    consume,
    current_budget,
    current_parent,
)
from autoscholar.orchestration.models import PlanStep, ReviewResult, TaskPlan
from autoscholar.orchestration.repository import WorkflowRepository
from autoscholar.rag import database_models as rag_database_models  # noqa: F401
from tests.test_agent_runner import ScriptedProvider, response


def plan() -> TaskPlan:
    return TaskPlan(
        goal="Compare MNIST models",
        steps=[
            PlanStep(id="code", type="coding", description="Code", expected_output="Sources"),
            PlanStep(
                id="train",
                type="experiment",
                description="Train",
                dependencies=["code"],
                expected_output="Metrics",
            ),
        ],
    )


@pytest.mark.parametrize("case", ["cycle", "unknown", "duplicate", "missing_code"])
def test_plan_rejects_invalid_dependencies(case: str) -> None:
    data = plan().model_dump()
    if case == "cycle":
        data["steps"][0]["dependencies"] = ["train"]
    elif case == "unknown":
        data["steps"][0]["dependencies"] = ["missing"]
    elif case == "duplicate":
        data["steps"].append(data["steps"][0])
    else:
        data["steps"][1]["dependencies"] = []
    with pytest.raises(ValueError):
        TaskPlan.model_validate(data)


async def test_budget_stops_calls_and_keeps_parallel_contexts_isolated() -> None:
    provider = ScriptedProvider([response(), response()])
    metered = BudgetedLLM(provider)

    async def one_task() -> None:
        budget = Budget(BudgetLimits(model_calls=1))
        token = current_budget.set(budget)
        try:
            await metered.generate([])
            await asyncio.sleep(0)
            with pytest.raises(BudgetExceeded):
                await metered.generate([])
            assert budget.used["model_calls"] == 1
        finally:
            current_budget.reset(token)

    await asyncio.gather(one_task(), one_task())
    assert len(provider.calls) == 2


def test_global_repair_limit_does_not_reset_between_steps() -> None:
    token = current_budget.set(Budget(BudgetLimits(code_repairs=1)))
    try:
        consume("code_repairs")
        with pytest.raises(BudgetExceeded):
            consume("code_repairs")
    finally:
        current_budget.reset(token)


async def test_workflow_persists_plan_versions_and_parent_link() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tasks = AgentTaskRepository(sessions)
    workflow = WorkflowRepository(sessions)
    await tasks.create_task(task_id="parent", objective="Compare", mode="autonomous")
    token = current_parent.set("parent")
    try:
        child = await tasks.create_task(task_id="child", objective="Code", mode="coding")
    finally:
        current_parent.reset(token)
    assert child.parent_task_id == "parent"
    await workflow.save_plan("parent", 1, plan(), "initial")
    revised = plan().model_copy(update={"goal": "Revised comparison"})
    await workflow.save_plan("parent", 2, revised, "review found missing result")
    run_id = await workflow.start_step("parent", 1, "code", "child")
    await workflow.finish_step(run_id, "succeeded", {"source_sha256": "a" * 64})
    await workflow.save_review("parent", 2, ReviewResult(status="PASS"))
    versions = await workflow.history("parent", "plans")
    assert {item["version"] for item in versions} == {1, 2}
    assert next(item for item in versions if item["version"] == 1)["payload"]["goal"] == (
        "Compare MNIST models"
    )
    assert (await workflow.history("parent", "steps"))[0]["status"] == "succeeded"
    assert (await workflow.history("parent", "reviews"))[0]["payload"]["status"] == "PASS"
    assert await workflow.history("other", "plans") == []
    await engine.dispose()
