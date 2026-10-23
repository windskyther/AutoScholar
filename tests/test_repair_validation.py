"""No vendor calls: actual SDK/HTTP parsing plus scripted durable/sandbox outcomes."""

import argparse
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.evaluation import repair_validation as repair
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.repair_protocol import (
    REPAIR_TASKS,
    RepairLedger,
    RepairWorkflowProvider,
    repair_contract,
)
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.errors import LLMResponseError
from autoscholar.llm.models import ChatMessage, ConversationMessage, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_autonomous import workflow
from tests.test_evaluation_workflow import RESOURCES, WorkflowReplaySandbox
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture
from tests.test_llm_provider import configured_settings
from tests.test_workflow_foundation import plan


@pytest.fixture
def wire_root() -> Iterator[Path]:
    # The entrypoint adds its real run/case/workspace hierarchy. Keep the test root
    # short enough for Windows directory limits, while retaining the real layout.
    parent = Path(__file__).resolve().parents[1] / "data"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix="rw-") as directory:
        result = Path(directory).resolve()
        assert result.is_relative_to(parent.resolve())
        yield result


def transport_for(tmp_path: Path, responses: list[tuple[str, str, str]]) -> GuardedTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_tokens"] == 8192
        name, arguments, finish = responses.pop(0)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "native-call",
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    return GuardedTransport(
        provider="llm",
        endpoint="https://llm.example/v1/chat/completions",
        ledger=RepairLedger(tmp_path / "grant.json", run_id="fake-only"),
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(handler),
    )


async def test_known_malformed_arguments_get_one_real_metered_correction(tmp_path: Path) -> None:
    transport = transport_for(
        tmp_path,
        [
            ("submit_task_plan", plan().model_dump_json() + " extra", "tool_calls"),
            ("submit_task_plan", plan().model_dump_json(), "tool_calls"),
            ("submit_code_ready", '{"summary":"Validated"}', "tool_calls"),
            ("submit_review", '{"status":"PASS"}', "tool_calls"),
        ],
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = RepairWorkflowProvider(
            OpenAICompatibleProvider(configured_settings(), http_client=client),
            transport,
            "normal-baseline",
        )
        service, engine = await workflow(tmp_path / "workspace", provider, FakeExperimentSandbox())
        try:
            result = await service.run("Compare models")
            assert result.task.status == "succeeded", result.task.error_message
            assert provider.corrections == 1 and not provider.failed
            assert (
                result.task.metrics["model_calls"]
                == provider.calls
                == len(transport.ledger.attempts)
                == 4
            )
            assert result.task.metrics["total_tokens"] == 20
            correction = json.loads(
                (tmp_path / "request-llm-normal-baseline--2.json").read_text(encoding="utf-8")
            )
            assert "extra_data" in correction["messages"][-1]["content"]
        finally:
            await engine.dispose()


@pytest.mark.parametrize("finish", ["length", "tool_calls"])
async def test_terminal_or_repeated_invalid_response_cannot_create_phantom_call(
    tmp_path: Path, finish: str
) -> None:
    transport = transport_for(tmp_path, [("submit_task_plan", "{} tail", finish)] * 2)
    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            provider = RepairWorkflowProvider(
                OpenAICompatibleProvider(configured_settings(), http_client=client),
                transport,
                "normal-baseline",
            )
            metered = BudgetedLLM(provider)
            for ordinal in range(1 if finish == "length" else 2):
                with pytest.raises(LLMResponseError) as caught:
                    await metered.generate(
                        [ChatMessage(role="user", content="Plan")],
                        tools=[
                            ToolDefinition(
                                name="submit_task_plan", description="Plan", parameters={}
                            )
                        ],
                    )
                assert caught.value.retryable is (finish != "length" and ordinal == 0)
            before = dict(budget.used)
            with pytest.raises(ValueError, match="stopped"):
                await metered.generate([ChatMessage(role="user", content="Do not dispatch")])
            assert budget.used == before
            assert budget.used["model_calls"] == len(transport.ledger.attempts) == provider.calls
            assert budget.used["total_tokens"] == 5 * provider.calls
    finally:
        current_budget.reset(token)


@pytest.mark.parametrize("task", REPAIR_TASKS)
async def test_three_repair_paths_have_durable_scripted_evidence(tmp_path: Path, task: str) -> None:
    from tests.integration.phase10_repairs_acceptance import UnnecessaryCodingRetryProvider

    data = fixture()
    scenario, _, variant = task.partition("-")
    script = data.scripts[scenario].model_copy(
        update={"prompt": data.scripts[scenario].prompt + SUFFIX}
    )
    transport = transport_for(tmp_path, [])
    scripted = (
        UnnecessaryCodingRetryProvider if task == "recover-baseline" else RenamedFixtureProvider
    )
    provider = RepairWorkflowProvider(
        scripted(data, script, project_source=PUBLIC_KNOWLEDGE),
        transport,
        task,
    )
    outcome = await execute_task(
        directory=tmp_path,
        fixture=data,
        script=script,
        variant="no_replanning" if variant == "no_replanning" else "baseline",
        provider=provider,
        sandbox=WorkflowReplaySandbox(),
        resources=RESOURCES,
        protocol_version=3,
    )
    assert outcome["status"] == "measured", outcome
    assert outcome["task_success"] is (variant != "no_replanning"), outcome
    assert outcome["step_counts"]["research"] == outcome["step_counts"]["code"] == 1
    assert not transport.ledger.attempts  # Explicit scripted model, never vendor traffic.
    saved = json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))
    started = {e["payload"]["call_id"] for e in saved["events"] if e["kind"] == "call_started"}
    finished = {e["payload"]["call_id"] for e in saved["events"] if e["kind"] == "call_finished"}
    assert started == finished


def test_repair_ledger_forbids_other_tasks_searches_and_resets(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "grant.json", run_id="fake-only")
    for task in REPAIR_TASKS:
        for ordinal in range(1, 13):
            ledger.reserve("llm", f"{task}--{ordinal}", {})
        with pytest.raises(ValueError):
            ledger.reserve("llm", f"{task}--13", {})
    assert len(ledger.attempts) == 36
    with pytest.raises(ValueError):
        ledger.reserve("llm", "deception-baseline--1", {})
    with pytest.raises(ValueError):
        ledger.reserve("tavily", "normal-baseline--1", {})
    with pytest.raises(FileExistsError):
        RepairLedger(tmp_path / "grant.json", run_id="reset")


async def test_paid_entrypoint_requires_explicit_approval_before_any_io(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="separate approval"):
        await repair.run_repairs(argparse.Namespace(allow_paid=False), repo_root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_free_plan_does_not_initialize_settings_or_claim_paid_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Free preparation must not read credentials or initialize Docker")

    monkeypatch.setattr(repair, "load_explicit_settings", forbidden)
    monkeypatch.setattr(repair, "DockerEvaluationSandbox", forbidden)
    monkeypatch.setattr(repair, "_git_state", lambda root: ("a" * 40, False))
    monkeypatch.setattr(repair, "load_fixture", lambda *args: (None, "b" * 64))
    directory = repair.prepare_repairs(tmp_path)
    plan = repair.load_repair_plan(directory / "plan.json", tmp_path)
    assert plan["contract"] == repair_contract()
    assert plan["external_api_calls"] == 0 and not plan["paid_execution_approved"]
    assert not (tmp_path / "data/evaluation/live-repair-v3-claim.json").exists()
    monkeypatch.setattr(repair, "_git_state", lambda root: ("c" * 40, False))
    with pytest.raises(ValueError, match="changed"):
        repair.load_repair_plan(directory / "plan.json", tmp_path)


@pytest.mark.parametrize("missing", [None, "fault", "recovery", "reuse", "measurement"])
def test_repair_gate_requires_the_intended_scenario_not_just_a_failed_status(
    tmp_path: Path, missing: str | None
) -> None:
    ledger = RepairLedger(tmp_path / "grant.json", run_id="explicit-fake")
    records: list[dict[str, Any]] = []
    for task in REPAIR_TASKS:
        index = ledger.reserve("llm", task + "--1", {})
        ledger.finish(index, state="returned", status=200)
        training = 2 if task == "recover-baseline" else 1
        records.append(
            {
                "task": task,
                "status": "measured",
                "task_success": task != "recover-no_replanning",
                "error_code": "evaluation_replanning_disabled"
                if task == REPAIR_TASKS[-1]
                else None,
                "intervention_verified": True,
                "physical_accounting_verified": True,
                "input_sha256": "public-input",
                "observed_models": ["fake-model"],
                "fault_mutations": int(task != "normal-baseline"),
                "step_counts": {"research": 1, "code": 1, "train": training},
                "usage": {"training_runs": training},
            }
        )
    if missing == "fault":
        records[-1]["fault_mutations"] = 0
    elif missing == "recovery":
        records[1]["task_success"] = False
    elif missing == "reuse":
        records[1]["step_counts"]["code"] = 2
    elif missing == "measurement":
        records[-1]["task_success"] = None
    summary = repair.repair_summary(records, ledger, clean=True, unchanged=True)
    assert summary["targeted_repairs_acceptance"] is (missing is None)
    assert summary["phase10_overall_acceptance"] is False
    assert summary["main_merge_authorized"] is False


@pytest.mark.parametrize("version", [3, 4, 5])
async def test_complete_entrypoint_with_mock_wire_usage_and_scripted_training(
    wire_root: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    """The paid flag is tested only with a mock credential loader and mock HTTP transport."""
    tmp_path = wire_root
    data = fixture()
    prepared = {"contract": repair_contract(version), "git_revision": "a" * 40}
    root = tmp_path / "data/evaluation"
    root.mkdir(parents=True)
    prior_claim = root / "live-repair-v3-claim.json"
    if version >= 4:
        prior_claim.write_text('{"old_record":"must remain unchanged"}', encoding="utf-8")
    old_claim = prior_claim.read_bytes() if prior_claim.exists() else None
    scripted = RenamedFixtureProvider(data, data.scripts["normal"], project_source=PUBLIC_KNOWLEDGE)
    physical_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal physical_requests
        physical_requests += 1
        payload = json.loads(request.content)
        messages: list[ConversationMessage] = [
            ChatMessage(role=item["role"], content=item["content"]) for item in payload["messages"]
        ]
        tools = [
            ToolDefinition(
                name=item["function"]["name"],
                description=item["function"]["description"],
                parameters=item["function"]["parameters"],
                strict=item["function"]["strict"],
            )
            for item in payload["tools"]
        ]
        result = await scripted.generate(messages, tools=tools, tool_choice="auto")
        call = result.tool_calls[0]
        return httpx.Response(
            200,
            json={
                "id": "fake-wire",
                "object": "chat.completion",
                "created": 1,
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": json.dumps(call.arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    original_transport = GuardedTransport

    def fake_transport(**kwargs: Any) -> GuardedTransport:
        return original_transport(**kwargs, delegate=httpx.MockTransport(handler))

    async def no_children(sandbox: Any) -> bool:
        return True

    monkeypatch.setattr(repair, "load_repair_plan", lambda *args: prepared)
    monkeypatch.setattr(repair, "load_fixture", lambda *args: (data, "b" * 64))
    monkeypatch.setattr(
        repair, "load_explicit_settings", lambda *args, **kwargs: (configured_settings(), None)
    )
    monkeypatch.setattr(repair, "DockerEvaluationSandbox", lambda *args: WorkflowReplaySandbox())
    monkeypatch.setattr(repair, "sandbox_resources", lambda *args, **kwargs: RESOURCES)
    monkeypatch.setattr(repair, "no_owned_children", no_children)
    monkeypatch.setattr(repair, "GuardedTransport", fake_transport)
    monkeypatch.setattr(repair, "_git_state", lambda root: ("a" * 40, False))
    args = argparse.Namespace(
        allow_paid=True,
        plan=tmp_path / "explicit-fake-plan.json",
        approval_id="fake-wire-only",
        sandbox_container="unused-fake-controller",
        protocol_version=version,
    )
    directory, accepted = await repair.run_repairs(args, repo_root=tmp_path)
    assert accepted
    result = json.loads((directory / "acceptance.json").read_text(encoding="utf-8"))
    assert result["physical_requests"] == {"llm": physical_requests, "tavily": 0}
    assert result["vendor_tokens"] == physical_requests * 3
    assert result["targeted_repairs_acceptance"]
    assert result["protocol_version"] == version
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["explicit_paid_execution"] and manifest["prepared_plan"] == prepared
    assert result["phase10_overall_acceptance"] is False
    assert result["main_merge_authorized"] is False
    args.approval_id = "another-fake-id"
    with pytest.raises(ValueError, match="already claimed"):
        await repair.run_repairs(args, repo_root=tmp_path)
    assert len(list(root.glob(f"live-repairs-v{version}-*"))) == 1
    if old_claim is not None:
        assert prior_claim.read_bytes() == old_claim
