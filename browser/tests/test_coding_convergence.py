"""Budgeted coding hands off to real validation decisions, never to an implicit PASS."""

import json
from pathlib import Path
from typing import Any

import pytest

from autoscholar.coding.agent import CodingAgent, CodingLimits, CodingRunError
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import (
    Budget,
    BudgetedLLM,
    BudgetExceeded,
    BudgetLimits,
    current_budget,
)
from autoscholar.llm.models import LLMResult, TokenUsage, ToolCall
from tests.test_autonomous import call, script, workflow
from tests.test_coding_agent import FakeSandbox, repository, run_result
from tests.test_experiment_service import FakeExperimentSandbox


class EditingProvider:
    configured = True

    def __init__(self, *, failed_write: bool = False) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.failed_write = failed_write

    async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
        payload = json.loads(messages[-1].content)
        self.payloads.append(payload)
        source = payload["source_files"]["app.py"]
        call = ToolCall(
            id=f"edit-{len(self.payloads)}",
            name="edit_file",
            arguments={
                "path": "app.py",
                "old_text": "absent" if self.failed_write else source,
                "new_text": source + f"\n# edit {len(self.payloads)}\n",
            },
        )
        return LLMResult(
            text="", model="explicit-editing-script", usage=TokenUsage(4, 2, 6), tool_calls=(call,)
        )

    async def close(self) -> None:
        pass


async def setup_agent(tmp_path: Path, provider: Any, sandbox: FakeSandbox) -> Any:
    store, engine = await repository()
    await store.create_task(task_id="convergence", objective="Implement and validate")
    workspace = WorkspaceManager(tmp_path)
    workspace.initialize("convergence")
    workspace.write_text("convergence", "app.py", "value = 1\n")
    agent = CodingAgent(
        provider=BudgetedLLM(provider),
        repository=store,
        workspaces=workspace,
        sandbox=sandbox,
        limits=CodingLimits(max_repairs=0),
    )
    return agent, store, engine, workspace


@pytest.mark.parametrize("shared_budget", [False, True])
async def test_editing_script_converges_to_mandatory_checks_within_existing_budget(
    tmp_path: Path, shared_budget: bool
) -> None:
    provider = EditingProvider()
    sandbox = FakeSandbox([run_result(), run_result()])
    agent, store, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
    budget = Budget(BudgetLimits(model_calls=12), used={"model_calls": 5})
    token = current_budget.set(budget if shared_budget else None)
    try:
        result = await agent.run(
            task_id="convergence",
            objective="Implement and validate",
            plan=["Inspect source"],
            snapshot_mode=True,
            model_call_reserve=3 if shared_budget else 0,
        )
        expected = 4 if shared_budget else 8
        assert result.model_calls == result.files_written == len(provider.payloads) == expected
        assert [r.action for r in sandbox.requests] == ["static_check", "run_pytest"]
        assert (
            sandbox.requests[0].files
            == sandbox.requests[1].files
            == workspace.source_snapshot("convergence")
        )
        assert [p["coding_progress"]["edits_used"] for p in provider.payloads] == list(
            range(expected)
        )
        child = await store.get_task("convergence")
        reason = "model_call_reserve" if shared_budget else "edit_limit"
        checks = [t for t in child.tool_calls if t.tool_name in {"static_check", "run_pytest"}]
        assert len(checks) == 2 and all(t.arguments["handoff_reason"] == reason for t in checks)
        if shared_budget:
            assert budget.used["model_calls"] == 9  # Exactly three calls remain for workflow.
            assert provider.payloads[-1]["coding_progress"]["author_calls_remaining"] == 1
    finally:
        current_budget.reset(token)
        await engine.dispose()


async def test_no_first_author_call_is_charged_when_only_reserved_calls_remain(
    tmp_path: Path,
) -> None:
    provider = EditingProvider()
    sandbox = FakeSandbox([])
    agent, _, engine, _ = await setup_agent(tmp_path, provider, sandbox)
    budget = Budget(BudgetLimits(model_calls=8), used={"model_calls": 5})
    token = current_budget.set(budget)
    try:
        with pytest.raises(BudgetExceeded, match="reserved_for_workflow"):
            await agent.run(
                task_id="convergence",
                objective="Inspect",
                plan=[],
                snapshot_mode=True,
                model_call_reserve=3,
            )
        assert not provider.payloads and not sandbox.requests
        assert budget.used == {"model_calls": 5}
    finally:
        current_budget.reset(token)
        await engine.dispose()


@pytest.mark.parametrize("failure", ["static", "pytest", "nonzero", "truncated"])
async def test_budget_handoff_never_passes_failed_or_incomplete_validation(
    tmp_path: Path, failure: str
) -> None:
    failed = run_result("failed", stderr="AssertionError: source is incomplete", exit_code=1)
    results = {
        "static": [failed],
        "pytest": [run_result(), failed],
        "nonzero": [run_result().model_copy(update={"exit_code": 1})],
        "truncated": [run_result().model_copy(update={"truncated": True})],
    }[failure]
    provider = EditingProvider()
    sandbox = FakeSandbox(results)
    agent, store, engine, _ = await setup_agent(tmp_path, provider, sandbox)
    token = current_budget.set(Budget(BudgetLimits(model_calls=1)))
    try:
        with pytest.raises(CodingRunError, match="validation still failed"):
            await agent.run(task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True)
        assert len(provider.payloads) == 1
        child = await store.get_task("convergence")
        assert child.tool_calls[-1].status == "failed"
        assert not any(r.action == "run_python" for r in sandbox.requests)
    finally:
        current_budget.reset(token)
        await engine.dispose()


async def test_failed_edit_at_handoff_boundary_is_not_treated_as_code_ready(tmp_path: Path) -> None:
    provider = EditingProvider(failed_write=True)
    sandbox = FakeSandbox([])
    agent, _, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
    token = current_budget.set(Budget(BudgetLimits(model_calls=1)))
    try:
        with pytest.raises(CodingRunError, match="unresolved"):
            await agent.run(task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True)
        assert not sandbox.requests
        assert workspace.read_text("convergence", "app.py") == "value = 1\n"
    finally:
        current_budget.reset(token)
        await engine.dispose()


@pytest.mark.parametrize("repeat_invalid", [False, True])
async def test_snapshot_batch_is_atomic_and_receives_bounded_correction(
    tmp_path: Path, repeat_invalid: bool
) -> None:
    class BatchProvider(EditingProvider):
        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            payload = json.loads(messages[-1].content)
            self.payloads.append(payload)
            if len(self.payloads) > 1:
                assert "exactly one" in payload["protocol_feedback"]
            if len(self.payloads) == 1 or repeat_invalid:
                calls = tuple(
                    ToolCall(
                        id=f"write-{i}",
                        name="create_file",
                        arguments={"path": f"extra{i}.py", "content": "x = 1\n"},
                    )
                    for i in range(2)
                )
            else:
                calls = (
                    ToolCall(id="ready", name="submit_code_ready", arguments={"summary": "Ready"}),
                )
            return LLMResult(
                text="", model="explicit-batch-script", usage=TokenUsage(4, 2, 6), tool_calls=calls
            )

    provider = BatchProvider()
    sandbox = FakeSandbox([] if repeat_invalid else [run_result(), run_result()])
    agent, _, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
    try:
        if repeat_invalid:
            with pytest.raises(CodingRunError, match="no batch was executed"):
                await agent.run(
                    task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True
                )
            assert not sandbox.requests
        else:
            await agent.run(task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True)
        assert len(provider.payloads) == 2
        assert set(workspace.source_snapshot("convergence")) == {"app.py"}
    finally:
        await engine.dispose()


async def test_source_change_during_validation_cannot_be_handed_off(tmp_path: Path) -> None:
    provider = EditingProvider()
    sandbox = FakeSandbox([run_result(), run_result()])
    agent, _, engine, workspace = await setup_agent(tmp_path, provider, sandbox)
    original = sandbox.run

    async def tampering(request: Any) -> Any:
        result = await original(request)
        workspace.write_text("convergence", "app.py", "unverified = True\n", overwrite=True)
        return result

    sandbox.run = tampering  # type: ignore[method-assign]
    token = current_budget.set(Budget(BudgetLimits(model_calls=1)))
    try:
        with pytest.raises(CodingRunError, match="Source changed"):
            await agent.run(task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True)
    finally:
        current_budget.reset(token)
        await engine.dispose()


async def test_automatic_validation_does_not_override_a_real_unmet_requirement(
    tmp_path: Path,
) -> None:
    class WorkflowProvider(EditingProvider):
        async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
            name = kwargs["tools"][0].name
            if name == "submit_task_plan":
                return script()[0]
            if name == "submit_review":
                return call(
                    "submit_review",
                    {
                        "status": "REPLAN",
                        "suggested_steps": ["code"],
                        "issues": [
                            {
                                "step_id": "code",
                                "code": "wrong_provenance",
                                "check": "requirement",
                                "message": "The requested exact provenance file is wrong",
                                "source_path": "provenance.txt",
                                "source_quote": "unverified",
                                "requirement_quote": (
                                    "write provenance.txt containing exactly verified"
                                ),
                            }
                        ],
                    },
                )
            payload = json.loads(messages[-1].content)
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return call("create_file", {"path": "provenance.txt", "content": "unverified"})
            source = payload["source_files"]["train.py"]
            anchor = source.rstrip("\n").splitlines()[-1] + "\n"
            return call(
                "edit_file",
                {
                    "path": "train.py",
                    "old_text": anchor,
                    "new_text": anchor + f"\n# harmless edit {len(self.payloads)}\n",
                },
            )

    provider = WorkflowProvider()
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run(
            "Compare models and write provenance.txt containing exactly verified",
            budget=BudgetLimits(code_repairs=0, replans=0),
        )
        assert result.task.status == "budget_exceeded" and result.task.answer is None
        assert result.task.metrics["training_runs"] == 1
        review = (await service.workflows.history(result.task.id, "reviews"))[0]["payload"]
        assert review["status"] == "REPLAN" and review["issues"][0]["code"] == "wrong_provenance"
        assert len(provider.payloads) == 8
    finally:
        await engine.dispose()


async def test_minimal_finishable_workflow_does_not_reserve_an_unaffordable_optional_replan(
    tmp_path: Path,
) -> None:
    from tests.test_agent_runner import ScriptedProvider

    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    try:
        result = await service.run("Compare models", budget=BudgetLimits(model_calls=3))
        assert result.task.status == "succeeded", result.task.error_message
        assert result.task.metrics["model_calls"] == 3
        payload = json.loads(provider.calls[1]["messages"][-1].content)
        assert payload["coding_progress"]["model_calls_reserved_for_workflow"] == 1
    finally:
        await engine.dispose()
