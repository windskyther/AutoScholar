from pathlib import Path

import pytest

from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.memory import ProjectContext, ProjectMemoryUpdate
from autoscholar.rag.database_models import ProjectRow
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import InvalidOnceSandbox, script, workflow
from tests.test_durable_workflow import drain


async def test_verified_experience_is_reused_only_inside_project(tmp_path: Path) -> None:
    provider = ScriptedProvider([*script(replan=True), *script(), *script()])
    service, engine = await workflow(tmp_path, provider, InvalidOnceSandbox())
    durable = DurableService(service)
    try:
        async with durable.repository.sessions() as session:
            session.add_all([ProjectRow(id="p1", name="First"), ProjectRow(id="p2", name="Second")])
            await session.commit()
        await durable.memory.update(
            "p1",
            ProjectMemoryUpdate(
                expected_version=0,
                context=ProjectContext(research_topic="MNIST comparison"),
            ),
        )
        first, _ = await durable.submit({"objective": "Compare", "project_id": "p1"}, "first")
        assert await drain(durable, first) == "succeeded"
        memories = await durable.memory.experiences("p1")
        assert len(memories) == 1
        experience = memories[0]
        assert experience["problem_code"] == "experiment_metrics_invalid"
        assert experience["source_task_id"] == first
        assert experience["evidence"]["verified"] is True
        assert experience["evidence"]["experiment_id"]
        assert await durable.memory.experiences("p2") == []
        second, _ = await durable.submit(
            {"objective": "Compare again", "project_id": "p1"}, "second"
        )
        assert await drain(durable, second) == "succeeded"
        snapshot, _ = await durable.repository.snapshot(second)
        assert snapshot.memory_context["experiences"][0]["id"] == experience["id"]
        assert snapshot.memory_context["project"]["context"]["research_topic"] == "MNIST comparison"
        assert len(await durable.memory.experiences("p1")) == 1  # no failure -> no new claim
        third, _ = await durable.submit({"objective": "Unrelated", "project_id": "p2"}, "third")
        assert await drain(durable, third) == "succeeded"
        snapshot, _ = await durable.repository.snapshot(third)
        assert snapshot.memory_context["experiences"] == []
        with pytest.raises(AppError):
            await durable.memory.set_enabled("p2", experience["id"], False)
        await durable.memory.set_enabled("p1", experience["id"], False)
        assert await durable.memory.experiences("p1") == []
        assert len(await durable.memory.experiences("p1", include_disabled=True)) == 1
    finally:
        await engine.dispose()


async def test_project_memory_rejects_lost_updates_and_unscoped_tasks(tmp_path: Path) -> None:
    service, engine = await workflow(tmp_path, ScriptedProvider(script()), InvalidOnceSandbox())
    durable = DurableService(service)
    try:
        async with durable.repository.sessions() as session:
            session.add(ProjectRow(id="p", name="Memory"))
            await session.commit()
        change = ProjectMemoryUpdate(expected_version=0, context=ProjectContext())
        assert (await durable.memory.update("p", change))["version"] == 1
        with pytest.raises(AppError, match="changed"):
            await durable.memory.update("p", change)
        task_id, _ = await durable.submit({"objective": "No project"}, "unscoped")
        await durable.tick()
        snapshot, _ = await durable.repository.snapshot(task_id)
        assert snapshot.memory_context == {}
        assert await durable.memory.experiences("p") == []
    finally:
        await engine.dispose()
