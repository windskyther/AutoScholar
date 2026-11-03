"""Reject a trial's wrong root DAG before children; never rewrite the model's plan."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.repaired_paired import RepairedPairedLedger
from autoscholar.evaluation.workflow_fixture import workflow_plan
from autoscholar.llm.errors import LLMResponseError
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.orchestration.models import TaskPlan
from tests.test_autonomous import workflow
from tests.test_coding_schema_feedback import completion
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_live_ablation_v2 import fixture
from tests.test_llm_provider import configured_settings


@pytest.mark.parametrize("case", ["knowledge", "extra", "dependency", "repeat", "quota", "general"])
async def test_fixed_trial_plan_gets_one_metered_correction_before_any_child(
    tmp_path: Path, case: str
) -> None:
    good = workflow_plan(fixture().scripts["recover"]).model_dump(mode="json")
    bad = copy.deepcopy(good)
    if case == "extra":
        bad["steps"].append(
            {
                "id": "extra",
                "type": "knowledge",
                "description": "Extra",
                "expected_output": "Extra",
                "dependencies": [],
            }
        )
    elif case == "dependency":
        bad["steps"][1]["dependencies"] = []
    else:
        bad["steps"][0]["type"] = "knowledge"
    TaskPlan.model_validate(bad)  # General platform validity is not the fixed trial contract.
    requests = []
    sandbox = FakeExperimentSandbox()

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert not sandbox.requests
        if len(requests) == 2:
            assert "required step types and dependencies" in body["messages"][-1]["content"]
        payload = bad if len(requests) == 1 or case == "repeat" else good
        return httpx.Response(200, json=completion([("submit_task_plan", payload)]))

    ledger = RepairedPairedLedger(
        tmp_path / "mock-ledger.json", run_id="offline-only", protocol_version=13
    )
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://llm.example/v1/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(respond),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        native = OpenAICompatibleProvider(configured_settings(), http_client=client)
        provider = RepairWorkflowProvider(
            native, transport, "recover-baseline", require_three_stage_plan=case != "general"
        )
        if case == "quota":
            provider.corrections = 1
        service, engine = await workflow(tmp_path / "workspace", provider, sandbox)
        budget = Budget(BudgetLimits(model_calls=12))
        token = current_budget.set(budget)
        try:

            async def plan_call() -> TaskPlan:
                return await service._structured(
                    "submit_task_plan",
                    TaskPlan,
                    "Use exactly research then coding then experiment, with linear dependencies.",
                    {"objective": "Research public evidence, inspect source, then train"},
                )

            if case in {"repeat", "quota"}:
                with pytest.raises(LLMResponseError) as caught:
                    await plan_call()
                assert not caught.value.retryable and provider.failed
                assert caught.value.validation_reason == "workflow_plan_contract"
            else:
                plan = await plan_call()
                assert plan.model_dump(mode="json") == (bad if case == "general" else good)
                assert not provider.failed
                assert provider.corrections == (0 if case == "general" else 1)
            expected = 1 if case in {"general", "quota"} else 2
            assert (
                len(requests)
                == provider.calls
                == len(ledger.attempts)
                == budget.used["model_calls"]
                == expected
            )
            assert budget.used["total_tokens"] == 3 * expected
            assert not sandbox.requests and not list((tmp_path / "workspace").rglob("train.py"))
            assert all(a["state"] == "returned" for a in ledger.attempts)
        finally:
            current_budget.reset(token)
            await engine.dispose()
