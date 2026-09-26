"""Restart, accounting and lifecycle tests use a real file-backed database and fake APIs."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from autoscholar.core.errors import AppError
from autoscholar.llm import LLMResult
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow
from autoscholar.orchestration.durable_repository import LeaseLost
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_experiment_service import FakeExperimentSandbox


async def drain(durable: DurableService, task_id: str) -> str:
    for _ in range(20):
        if not await durable.tick():
            break
    _, job = await durable.repository.snapshot(task_id)
    return job.status


async def test_restart_reuses_completed_coding_and_budget(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    sandbox = FakeExperimentSandbox()
    service, engine = await workflow(
        tmp_path / "workspace",
        provider,
        sandbox,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
    )
    durable = DurableService(service)
    try:
        task_id, created = await durable.submit(
            {"objective": "Compare", "mode": "autonomous"}, "one"
        )
        assert created
        assert await durable.tick()  # planner
        assert await durable.tick()  # coding
        before, job = await durable.repository.snapshot(task_id)
        assert "code" in before.results
        assert job.usage["model_calls"] == 2
        restarted = DurableService(service)
        assert await drain(restarted, task_id) == "succeeded"
        task = await service.tasks.get_task(task_id)
        assert task and task.metrics["model_calls"] == 3 and task.metrics["training_runs"] == 1
        steps = await service.workflows.history(task_id, "steps")
        assert len(steps) == 2
        training = next(item for item in steps if item["step_id"] == "train")
        assert len(await service.tasks.list_artifacts(training["child_task_id"])) == 10
    finally:
        await engine.dispose()


@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_control_during_active_call_is_bounded_and_durable(
    tmp_path: Path,
    action: str,
) -> None:
    class HeldProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__(script())
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, *args: Any, **kwargs: Any) -> LLMResult:
            self.entered.set()
            await self.release.wait()
            return await super().generate(*args, **kwargs)

    provider = HeldProvider()
    service, engine = await workflow(
        tmp_path / "workspace",
        provider,
        FakeExperimentSandbox(),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.sqlite'}",
    )
    durable = DurableService(service, lease_seconds=6)
    execution: asyncio.Task[bool] | None = None
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, action)
        execution = asyncio.create_task(durable.tick(task_id))
        await asyncio.wait_for(provider.entered.wait(), 5)
        assert await durable.repository.control(task_id, action) == f"{action}_requested"
        if action == "pause":
            provider.release.set()
        await asyncio.wait_for(execution, 6)
        snapshot, job = await durable.repository.snapshot(task_id)
        assert job.status == ("paused" if action == "pause" else "cancelled")
        assert job.usage["model_calls"] == 1
        assert not await durable.tick(task_id)
        if action == "pause":
            assert snapshot.stage == "executor"
            await durable.repository.control(task_id, "resume")
            assert await drain(durable, task_id) == "succeeded"
        else:
            assert job.pending_calls  # cancellation does not invent provider usage
            assert not provider.calls
    finally:
        if execution is not None and not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await engine.dispose()


async def test_submit_idempotency_pause_resume_and_cancel(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    durable = DurableService(service)
    payload = {"objective": "Compare", "mode": "autonomous"}
    try:
        task_id, _ = await durable.submit(payload, "stable-key")
        assert await durable.submit(payload, "stable-key") == (task_id, False)
        with pytest.raises(AppError, match="different request"):
            await durable.submit(payload | {"objective": "Other"}, "stable-key")
        assert await durable.repository.control(task_id, "pause") == "paused"
        assert not await durable.tick()
        assert not provider.calls
        assert await durable.repository.control(task_id, "resume") == "queued"
        assert await durable.repository.control(task_id, "resume") == "queued"
        assert await durable.tick()
        assert await durable.repository.control(task_id, "cancel") == "cancelled"
        assert not await durable.tick()
        with pytest.raises(AppError):
            await durable.repository.control(task_id, "resume")
    finally:
        await engine.dispose()


async def test_expired_lease_with_unknown_paid_call_stops_without_replay(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "unknown")
        claimed = await durable.repository.claim(durable.owner, 30)
        assert claimed
        await durable.repository.begin(task_id, durable.owner, claimed[1], {"stage": "planner"})
        await durable.repository.journal(
            task_id, durable.owner, claimed[1], "call", "llm", True, {"model_calls": 1}, 1
        )
        async with durable.repository.sessions() as session:
            row = await session.get(WorkflowJobRow, task_id)
            assert row
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        restarted = DurableService(service)
        assert await restarted.tick()
        _, job = await durable.repository.snapshot(task_id)
        assert job.status == "recovery_required" and job.usage["model_calls"] == 1
        assert not provider.calls
        with pytest.raises(LeaseLost):
            await durable.repository.heartbeat(task_id, durable.owner, claimed[1], 30)
        with pytest.raises(AppError):
            await durable.repository.control(task_id, "resume")
    finally:
        await engine.dispose()


async def test_resume_detects_source_tampering(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "tamper")
        await durable.tick()
        await durable.tick()
        snapshot, _ = await durable.repository.snapshot(task_id)
        service.workspace.write_text(
            snapshot.results["code"]["child_task_id"], "train.py", "bad", overwrite=True
        )
        assert await drain(durable, task_id) == "recovery_required"
        assert len(provider.calls) == 2
    finally:
        await engine.dispose()


async def test_checkpoint_hash_and_budget_survive_pause(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit(
            {"objective": "Compare", "budget": {"model_calls": 1}}, "cap"
        )
        await durable.tick()
        await durable.repository.control(task_id, "pause")
        await durable.repository.control(task_id, "resume")
        assert await drain(durable, task_id) == "budget_exceeded"
        assert len(provider.calls) == 1
        async with durable.repository.sessions() as session:
            row = await session.scalar(
                select(WorkflowCheckpointRow)
                .where(
                    WorkflowCheckpointRow.task_id == task_id,
                )
                .order_by(WorkflowCheckpointRow.sequence.desc())
            )
            assert row
            row.payload = dict(row.payload) | {"objective": "tampered"}
            await session.commit()
        with pytest.raises(AppError, match="integrity"):
            await durable.repository.snapshot(task_id)
    finally:
        await engine.dispose()


async def test_completed_step_reconciles_after_checkpoint_commit_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "commit-interrupted")
        await durable.tick()

        async def interrupted_commit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Simulated process loss before checkpoint commit")

        monkeypatch.setattr(durable.repository, "save", interrupted_commit)
        with pytest.raises(RuntimeError, match="process loss"):
            await durable.tick()
        async with durable.repository.sessions() as session:
            row = await session.get(WorkflowJobRow, task_id)
            assert row and not row.pending_calls and row.active["step_id"] == "code"
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        restarted = DurableService(service)
        assert await drain(restarted, task_id) == "succeeded"
        assert len(provider.calls) == 3
        steps = await service.workflows.history(task_id, "steps")
        assert len([step for step in steps if step["step_id"] == "code"]) == 1
    finally:
        await engine.dispose()
