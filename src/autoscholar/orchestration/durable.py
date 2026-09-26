"""One leased, checkpointed workflow unit at a time, using Phase 6 execution services."""

import asyncio
import time
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from autoscholar.core.budget import BudgetExceeded, BudgetLimits, current_budget
from autoscholar.core.errors import AppError
from autoscholar.core.journal import current_journal
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.orchestration.checkpoints import Snapshot, Stage, digest
from autoscholar.orchestration.durable_repository import DurableRepository, LeaseLost, conflict
from autoscholar.orchestration.models import ReviewResult, TaskPlan
from autoscholar.orchestration.service import AutonomousService, FlowState, Run, source_digest


class DurableService:
    def __init__(
        self,
        service: AutonomousService,
        *,
        lease_seconds: int = 30,
        approval_threshold: int = 20000,
    ) -> None:
        self.service = service
        self.repository = DurableRepository(service.tasks.session_factory)
        self.lease_seconds = lease_seconds
        self.approval_threshold = approval_threshold
        self.owner = str(uuid4())

    async def submit(self, payload: dict[str, Any], key: str) -> tuple[str, bool]:
        request = dict(payload)
        request.pop("mode", None)
        requested_budget = request.pop("budget", None)
        specification = request.pop("experiment_specification", None)
        snapshot = Snapshot(
            **request,
            task_id=str(uuid4()),
            limits=BudgetLimits.model_validate(requested_budget or {}).bounded_by(
                self.service.limits
            ),
            specification=ExperimentSpecification.model_validate(specification or {}),
            approval_threshold=self.approval_threshold,
        )
        return await self.repository.enqueue(snapshot, key, digest(payload))

    async def tick(self) -> bool:
        claimed = await self.repository.claim(self.owner, self.lease_seconds)
        if claimed is None:
            return False
        task_id, generation = claimed
        execution = asyncio.create_task(self._execute(task_id, generation))
        try:
            while not execution.done():
                done, _ = await asyncio.wait({execution}, timeout=self.lease_seconds / 3)
                if done:
                    break
                status = await self.repository.heartbeat(
                    task_id,
                    self.owner,
                    generation,
                    self.lease_seconds,
                )
                if status == "cancel_requested":
                    execution.cancel()
            await execution
        except LeaseLost:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        return True

    async def _execute(self, task_id: str, generation: int) -> None:
        try:
            snapshot, job = await self.repository.snapshot(task_id)
        except (AppError, ValidationError):
            await self.repository.quarantine(task_id, self.owner, generation)
            return
        run = snapshot.restore(job.usage, job.active_seconds)
        run.budget.limits = run.budget.limits.bounded_by(self.service.limits)

        async def journal(call_id: str, kind: str, starting: bool) -> None:
            await self.repository.journal(
                task_id,
                self.owner,
                generation,
                call_id,
                kind,
                starting,
                run.budget.used,
                time.monotonic() - run.budget.started,
            )

        budget_token = current_budget.set(run.budget)
        journal_token = current_journal.set(journal)
        stage = snapshot.stage
        status, code = "queued", None
        try:
            if job.status == "cancel_requested":
                status = "cancelled"
            elif job.pending_calls:
                status, code = "recovery_required", "external_result_uncertain"
            elif job.active:
                stage = await self._reconcile(snapshot, run, job.active)
            elif job.status == "pause_requested":
                status = "paused"
            else:
                self._verify_sources(run)
                run.budget.check()
                active: dict[str, Any] = {"stage": stage, "version": run.version}
                if stage == "executor":
                    assert run.plan is not None
                    ready = next(
                        (
                            step
                            for step in run.plan.steps
                            if step.id not in run.results
                            and set(step.dependencies) <= run.results.keys()
                        ),
                        None,
                    )
                    if ready is not None:
                        active["step_id"] = ready.id
                state = await self.repository.begin(task_id, self.owner, generation, active)
                if state != "running":
                    status = "cancelled" if state == "cancel_requested" else "paused"
                else:
                    remaining = run.budget.limits.wall_seconds - (
                        time.monotonic() - run.budget.started
                    )
                    async with asyncio.timeout(max(0, remaining)):
                        stage = await self._unit(run, stage)
                    if stage == "done":
                        status = "succeeded"
        except (BudgetExceeded, TimeoutError):
            status, code = "budget_exceeded", "autonomous_budget_exceeded"
        except asyncio.CancelledError:
            # Graceful shutdown still records an interrupted unit. A user cancellation wins in save.
            status, code = "recovery_required", "workflow_interrupted"
        except LeaseLost:
            raise
        except Exception as exc:
            if getattr(exc, "code", "") in {"operation_uncertain", "checkpoint_integrity_failed"}:
                status = "recovery_required"
            else:
                status = "failed"
            code = str(getattr(exc, "code", "workflow_execution_failed"))
        finally:
            current_journal.reset(journal_token)
            current_budget.reset(budget_token)
        await self.repository.save(
            task_id,
            self.owner,
            generation,
            snapshot.capture(run, stage),
            run.budget.used,
            time.monotonic() - run.budget.started,
            status=status,
            error_code=code,
        )

    def _verify_sources(self, run: Run) -> None:
        for step_id, source in run.sources.items():
            result = run.results.get(step_id, {})
            if source_digest(source) != result.get("source_sha256") or source != (
                self.service.workspace.source_snapshot(result["child_task_id"])
            ):
                raise conflict("checkpoint_integrity_failed", "Source changed since checkpoint")

    async def _unit(self, run: Run, stage: Stage) -> Stage:
        state: FlowState = {"run": run}
        if stage == "planner":
            await self.service._planner(state)
            return "executor"
        if stage == "executor":
            assert run.plan is not None
            ready = next(
                (
                    step
                    for step in run.plan.steps
                    if step.id not in run.results and set(step.dependencies) <= run.results.keys()
                ),
                None,
            )
            if ready is not None:
                run.budget.consume("steps")
                await self.service._step(run, ready)
            return "reviewer" if len(run.results) == len(run.plan.steps) else "executor"
        if stage == "reviewer":
            await self.service._reviewer(state)
            assert run.review is not None
            return "writer" if run.review.status == "PASS" else "replanner"
        if stage == "replanner":
            await self.service._replanner(state)
            return "executor"
        if stage == "writer":
            if await self.service._rules(run):
                raise conflict(
                    "checkpoint_integrity_failed", "Final artifacts changed after review"
                )
            await self.service._writer(state)
            return "done"
        return "done"

    async def _reconcile(self, snapshot: Snapshot, run: Run, active: dict[str, Any]) -> Stage:
        if active.get("stage") == "executor":
            steps = await self.service.workflows.history(run.task_id, "steps")
            matches = [
                item
                for item in steps
                if item["plan_version"] == run.version and item["step_id"] == active.get("step_id")
            ]
            if len(matches) == 1 and matches[0]["status"] in {"succeeded", "failed"}:
                item = matches[0]
                outcome = item["result"]
                if outcome.get("error_code") == "autonomous_budget_or_cancelled":
                    raise conflict("operation_uncertain", "Interrupted child requires inspection")
                run.results[item["step_id"]] = outcome
                if outcome.get("source_sha256") and "experiment_id" not in outcome:
                    source = self.service.workspace.source_snapshot(item["child_task_id"])
                    if source_digest(source) != outcome["source_sha256"]:
                        raise conflict("checkpoint_integrity_failed", "Recovered source changed")
                    run.sources[item["step_id"]] = source
                task = await self.service.tasks.get_task(run.task_id)
                run.traces = (
                    max((call.sequence for call in task.tool_calls), default=0) if task else 0
                )
                return "executor"
        if active.get("stage") == "planner":
            plans = await self.service.workflows.history(run.task_id, "plans")
            matches = [item for item in plans if item["version"] == run.version]
            if len(matches) == 1:
                run.plan = TaskPlan.model_validate(matches[0]["payload"])
                self.service._validate_plan(run, run.plan)
                return "executor"
        if active.get("stage") == "reviewer":
            reviews = await self.service.workflows.history(run.task_id, "reviews")
            matches = [item for item in reviews if item["plan_version"] == run.version]
            if len(matches) == 1:
                run.review = ReviewResult.model_validate(matches[0]["payload"])
                return "writer" if run.review.status == "PASS" else "replanner"
        if active.get("stage") == "writer":
            return "writer"
        raise conflict(
            "operation_uncertain", "No durable completion record; automatic replay stopped"
        )
