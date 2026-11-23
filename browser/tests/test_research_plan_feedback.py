"""Original-schema validation and bounded feedback use only local HTTP fixtures."""

import json
from typing import Any

import httpx
import pytest

from autoscholar.agent.runner import AgentRunError, AgentRunner
from autoscholar.core.budget import (
    Budget,
    BudgetedLLM,
    BudgetExceeded,
    BudgetLimits,
    current_budget,
)
from autoscholar.core.errors import AppError
from autoscholar.core.journal import current_journal
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.research import SourceType
from tests.test_agent_runner import ScriptedProvider
from tests.test_coding_schema_feedback import completion
from tests.test_llm_provider import configured_settings
from tests.test_research_agent import repository, search_services, successful_responses


@pytest.mark.parametrize(
    "stage", ["submit_plan", "submit_research_queries", "submit_evidence", "submit_research_report"]
)
@pytest.mark.parametrize(
    "stop", [None, "repeat", "truncated", "invalid_json", "missing_usage", "transport", "budget"]
)
async def test_plan_feedback_is_bounded_metered_and_never_replays_unknown_results(
    stage: str,
    stop: str | None,
) -> None:
    requests: list[dict[str, Any]] = []
    script = successful_responses()
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        payload = json.loads(request.content)
        requests.append(payload)
        name = payload["tools"][0]["function"]["name"] if payload.get("tools") else None
        if name == stage:
            attempts += 1
        if name == stage and (attempts == 1 or stop == "repeat"):
            if stop == "transport":
                raise httpx.ReadTimeout("do not log raw request", request=request)
            invalid_payloads: dict[str, dict[str, Any]] = {
                "submit_plan": {"mode": "research", "steps": ["untrusted-value"] * 9},
                "submit_research_queries": {"queries": []},
                "submit_evidence": {
                    "items": [
                        {"candidate_id": "C1", "claim": "untrusted-value", "relevance": 0.9}
                        for _ in range(13)
                    ]
                },
                "submit_research_report": {"answer": "untrusted-value", "citations": []},
            }
            bad = invalid_payloads[stage]
            response = completion([(stage, bad)])
            if stop == "truncated":
                response["choices"][0]["finish_reason"] = "length"
            elif stop == "invalid_json":
                response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{"
            elif stop == "missing_usage":
                response.pop("usage")
            return httpx.Response(200, json=response)
        result = script.pop(0)
        response = completion([(call.name, call.arguments) for call in result.tool_calls])
        if not result.tool_calls:
            response["choices"][0]["message"]["content"] = result.text
            response["choices"][0]["finish_reason"] = "stop"
        return httpx.Response(200, json=response)

    pending: dict[str, str] = {}

    async def journal(call_id: str, kind: str, starting: bool) -> None:
        if starting:
            assert not pending  # a new dispatch must never follow an unresolved request
            pending[call_id] = kind
        else:
            assert pending.pop(call_id) == kind

    before = {
        "submit_plan": 0,
        "submit_research_queries": 1,
        "submit_evidence": 2,
        "submit_research_report": 3,
    }[stage]
    budget = Budget(BudgetLimits(model_calls=before + 1 if stop == "budget" else 20))
    budget_token = current_budget.set(budget)
    journal_token = current_journal.set(journal)
    store, engine = await repository()
    settings = configured_settings()
    settings.llm_compatibility_profile = "deepseek"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        runner = AgentRunner(
            provider=BudgetedLLM(OpenAICompatibleProvider(settings, http_client=client)),
            repository=store,
            tools=[],
            research_services=search_services(),
        )
        try:
            if stop is None:
                result = await runner.run(
                    "Compare public methods", task_id="feedback", mode="research"
                )
                assert result.task.status == "succeeded"
                assert result.task.metrics["model_calls"] == len(requests) == 5
                assert result.task.metrics["total_tokens"] == budget.used["total_tokens"] == 15
                assert budget.used["model_calls"] == 5 and attempts == 2
                correction = requests[before + 1]["messages"][-1]["content"]
                assert "Protocol correction" in correction and "untrusted-value" not in correction
                assert (
                    "maxItems" if stage in {"submit_plan", "submit_evidence"} else "minItems"
                ) in correction
                assert not pending
            else:
                with pytest.raises((AgentRunError, BudgetExceeded)):
                    await runner.run("Compare public methods", task_id="feedback", mode="research")
                expected = before + (2 if stop == "repeat" else 1)
                assert len(requests) == budget.used["model_calls"] == expected
                assert attempts == (2 if stop == "repeat" else 1)
                if stop not in {"missing_usage", "transport"}:
                    assert not pending
                    assert budget.used["total_tokens"] == expected * 3
                    task = await store.get_task("feedback")
                    assert task and task.metrics["model_calls"] == expected
                    assert task.metrics["total_tokens"] == expected * 3
                    if stop == "repeat":
                        assert "schema /properties/" in (task.error_message or "")
                        assert "untrusted-value" not in (task.error_message or "")
                else:
                    assert len(pending) == 1
                task = await store.get_task("feedback")
                assert task
                assert len(task.tool_calls) == (3 if before >= 2 else 0)
                assert len(task.evidence) == (3 if before >= 3 else 0)
            if before >= 2:
                assert budget.used["search_queries"] == 3  # correction never repeats searches
        finally:
            await engine.dispose()
            current_journal.reset(journal_token)
            current_budget.reset(budget_token)


async def test_predispatch_uncertainty_is_propagated_without_search_failure_trace() -> None:
    class BlockedSearch:
        name = "tavily"
        source_type: SourceType = "web"
        configured = True

        async def search(self, *args: Any, **kwargs: Any) -> Any:
            raise AppError(
                status_code=409,
                code="operation_uncertain",
                message="Previous external result is uncertain",
            )

        async def close(self) -> None:
            return None

    script = successful_responses()[:2]
    for query in script[1].tool_calls[0].arguments["queries"]:
        query["source_type"] = "web"
    store, engine = await repository()
    runner = AgentRunner(
        provider=ScriptedProvider(script),
        repository=store,
        tools=[],
        research_services=[BlockedSearch()],
    )
    try:
        with pytest.raises(AgentRunError) as caught:
            await runner.run(
                "Find public sources", task_id="blocked", mode="research", research_sources=["web"]
            )
        assert caught.value.code == "operation_uncertain"
        task = await store.get_task("blocked")
        assert task and not task.tool_calls
    finally:
        await engine.dispose()
