"""The evaluation guard permits one metered schema correction, never a new grant."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.core.budget import Budget, BudgetExceeded, BudgetLimits, current_budget
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.repaired_paired import RepairedPairedLedger
from autoscholar.llm.errors import LLMResponseError
from autoscholar.llm.models import TokenUsage
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_coding_agent import FakeSandbox, run_result
from tests.test_coding_convergence import setup_agent
from tests.test_coding_schema_feedback import completion, ready
from tests.test_llm_provider import configured_settings


@pytest.mark.parametrize(
    "stop", [None, "repeat", "truncated", "missing_usage", "mismatch", "used_correction"]
)
async def test_coding_schema_feedback_obeys_physical_receipts_and_shared_correction_quota(
    tmp_path: Path, stop: str | None
) -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = completion(
            [ready("x" * 336)] if len(requests) == 1 or stop == "repeat" else [ready()]
        )
        if stop == "truncated":
            payload["choices"][0]["finish_reason"] = "length"
        if stop == "missing_usage":
            payload.pop("usage")
        return httpx.Response(200, json=payload)

    grant = tmp_path / "mock-ledger.json"
    ledger = RepairedPairedLedger(grant, run_id="offline-only", protocol_version=12)
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://llm.example/v1/chat/completions",
        ledger=ledger,
        output=tmp_path,
        secrets=(),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(respond),
    )
    config = configured_settings()
    config.llm_compatibility_profile = "deepseek"
    async with httpx.AsyncClient(transport=transport) as client:
        native = OpenAICompatibleProvider(config, http_client=client)
        if stop == "mismatch":
            generate = native.generate

            async def inconsistent_receipt(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await generate(*args, **kwargs)
                except LLMResponseError as exc:
                    exc.usage = TokenUsage(2, 2, 4)
                    raise

            native.generate = inconsistent_receipt  # type: ignore[method-assign]
        provider = RepairWorkflowProvider(native, transport, "normal-no_reviewer")
        if stop == "used_correction":
            provider.corrections = 1
        sandbox = FakeSandbox([run_result(), run_result()])
        agent, _, engine, workspace = await setup_agent(tmp_path / "workspace", provider, sandbox)
        budget = Budget(BudgetLimits(model_calls=12))
        token = current_budget.set(budget)
        try:
            if stop is None:
                result = await agent.run(
                    task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True
                )
                assert result.model_calls == 2 and result.total_tokens == 6
                assert provider.corrections == 1 and not provider.failed
                assert [r.action for r in sandbox.requests] == ["static_check", "run_pytest"]
                assert (
                    "local tool schema"
                    in json.loads(requests[1]["messages"][-1]["content"])["protocol_feedback"]
                )
            else:
                with pytest.raises((LLMResponseError, BudgetExceeded)):
                    await agent.run(
                        task_id="convergence", objective="Inspect", plan=[], snapshot_mode=True
                    )
                assert provider.failed and not sandbox.requests
                before = dict(budget.used)
                with pytest.raises(ValueError, match="stopped"):
                    provider.check_available()
                assert budget.used == before
            expected = 2 if stop in (None, "repeat") else 1
            assert (
                len(requests)
                == len(ledger.attempts)
                == provider.calls
                == budget.used["model_calls"]
                == expected
            )
            assert all(
                a["state"] == "returned" and a["http_status"] == 200 for a in ledger.attempts
            )
            if stop != "missing_usage":
                assert budget.used["total_tokens"] == (4 if stop == "mismatch" else 3 * expected)
            assert workspace.source_snapshot("convergence") == {"app.py": "value = 1\n"}
            assert json.loads(grant.read_text())["limits"] == {"llm": 180, "tavily": 0}
        finally:
            current_budget.reset(token)
            await engine.dispose()
