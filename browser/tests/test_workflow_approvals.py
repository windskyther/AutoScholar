from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autoscholar.core.errors import AppError
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.orchestration.approvals import ApprovalDecision
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowApprovalRow
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_durable_workflow import drain
from tests.test_experiment_service import FakeExperimentSandbox


async def test_approval_is_required_and_consumed_once(tmp_path: Path) -> None:
    provider, sandbox = ScriptedProvider(script()), FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    durable = DurableService(service, approval_threshold=0)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "approval")
        assert await drain(durable, task_id) == "awaiting_approval"
        assert not any(item.action == "run_python" for item in sandbox.requests)
        approval = (await durable.approvals.list(task_id))[0]
        with pytest.raises(AppError):
            await durable.repository.control(task_id, "resume")
        decision = ApprovalDecision(action="approve", operation_sha256=approval["operation_sha256"])
        assert await durable.approvals.decide(task_id, approval["id"], decision) == "paused"
        assert await durable.approvals.decide(task_id, approval["id"], decision) == "paused"
        assert not await durable.tick()
        await durable.repository.control(task_id, "resume")
        assert await drain(durable, task_id) == "succeeded"
        assert (await durable.approvals.list(task_id))[0]["status"] == "consumed"
        assert len([item for item in sandbox.requests if item.action == "run_python"]) == 1
        with pytest.raises(AppError):
            await durable.approvals.decide(task_id, approval["id"], decision)
    finally:
        await engine.dispose()


async def test_rejection_modification_and_stale_approval(tmp_path: Path) -> None:
    responses = script()
    responses.insert(2, responses[1])  # parameters changed: mandatory coding validation again
    provider, sandbox = ScriptedProvider(responses), FakeExperimentSandbox()
    service, engine = await workflow(tmp_path, provider, sandbox)
    durable = DurableService(service, approval_threshold=0)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "modify")
        await drain(durable, task_id)
        approval = (await durable.approvals.list(task_id))[0]
        await durable.approvals.decide(
            task_id,
            approval["id"],
            ApprovalDecision(
                action="reject",
                operation_sha256=approval["operation_sha256"],
                reason="Reduce epochs",
            ),
        )
        with pytest.raises(AppError):
            await durable.repository.control(task_id, "resume")
        await durable.approvals.decide(
            task_id,
            approval["id"],
            ApprovalDecision(
                action="modify",
                operation_sha256=approval["operation_sha256"],
                specification=ExperimentSpecification(epochs=1),
            ),
        )
        snapshot, job = await durable.repository.snapshot(task_id)
        assert snapshot.version == 2 and not snapshot.results
        assert job.usage["model_calls"] == 2
        await durable.repository.control(task_id, "resume")
        assert await drain(durable, task_id) == "awaiting_approval"
        with pytest.raises(AppError, match="changed"):
            await durable.approvals.decide(
                task_id,
                approval["id"],
                ApprovalDecision(
                    action="approve",
                    operation_sha256=approval["operation_sha256"],
                ),
            )
        new = next(
            item for item in await durable.approvals.list(task_id) if item["status"] == "pending"
        )
        assert new["operation"]["specification"]["epochs"] == 1
        assert new["operation_sha256"] != approval["operation_sha256"]
        assert not any(item.action == "run_python" for item in sandbox.requests)
    finally:
        await engine.dispose()


async def test_expired_or_cross_task_approval_cannot_run(tmp_path: Path) -> None:
    service, engine = await workflow(tmp_path, ScriptedProvider(script()), FakeExperimentSandbox())
    durable = DurableService(service, approval_threshold=0)
    try:
        task_id, _ = await durable.submit({"objective": "Compare"}, "expired")
        await drain(durable, task_id)
        approval = (await durable.approvals.list(task_id))[0]
        async with durable.repository.sessions() as session:
            row = await session.get(WorkflowApprovalRow, approval["id"])
            assert row
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        decision = ApprovalDecision(action="approve", operation_sha256=approval["operation_sha256"])
        with pytest.raises(AppError, match="expired"):
            await durable.approvals.decide(task_id, approval["id"], decision)
        other, _ = await durable.submit({"objective": "Other"}, "other")
        with pytest.raises(AppError, match="not found"):
            await durable.approvals.decide(other, approval["id"], decision)
    finally:
        await engine.dispose()
