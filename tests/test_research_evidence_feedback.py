"""Evidence/report protocol and semantic corrections share one two-response allowance."""

import json
from typing import Any

import httpx
import pytest

from autoscholar.agent.runner import AgentRunError, AgentRunner
from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.research import SearchResponse, SearchResult, SourceType
from tests.test_coding_schema_feedback import completion
from tests.test_llm_provider import configured_settings
from tests.test_research_agent import repository, search_services, successful_responses


@pytest.mark.parametrize("stage", ["submit_evidence", "submit_research_report"])
@pytest.mark.parametrize("first", ["schema", "semantic"])
async def test_schema_and_semantic_errors_share_the_existing_attempt_limit(
    stage: str,
    first: str,
) -> None:
    requests: list[dict[str, Any]] = []
    script = successful_responses()
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["tools"][0]["function"]["name"] == stage:
            attempts += 1
            schema_error = (attempts == 1) == (first == "schema")
            if stage == "submit_evidence":
                bad = {
                    "items": [
                        {"candidate_id": "C999", "claim": "Unsupported", "relevance": 0.9}
                        for _ in range(13 if schema_error else 1)
                    ]
                }
            else:
                bad = {
                    "answer": "Unknown source [E999].",
                    "citations": []
                    if schema_error
                    else [{"claim": "Unsupported", "evidence_ids": ["E999"]}],
                }
            return httpx.Response(200, json=completion([(stage, bad)]))
        result = script.pop(0)
        return httpx.Response(
            200, json=completion([(call.name, call.arguments) for call in result.tool_calls])
        )

    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
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
            with pytest.raises(AgentRunError):
                await runner.run("Compare public methods", task_id="mixed", mode="research")
            expected = 4 if stage == "submit_evidence" else 5
            assert len(requests) == budget.used["model_calls"] == expected
            assert attempts == 2 and budget.used["total_tokens"] == expected * 3
            assert budget.used["search_queries"] == 3
            task = await store.get_task("mixed")
            assert task and task.metrics["model_calls"] == expected
            assert task.metrics["total_tokens"] == expected * 3
            assert len(task.tool_calls) == 3
            assert len(task.evidence) == (0 if stage == "submit_evidence" else 3)
        finally:
            await engine.dispose()
            current_budget.reset(token)


async def test_fifteen_candidates_are_corrected_to_bounded_evidence_without_researching() -> None:
    requests: list[dict[str, Any]] = []
    searches: list[str] = []
    evidence_attempts = 0
    queries = [
        {
            "topic": f"Topic {i}",
            "query": f"Public query {i}",
            "source_type": "web",
            "purpose": "Find evidence",
        }
        for i in range(5)
    ]
    selected = [number for number in range(1, 16) if number % 3 != 0]

    class ManyResults:
        name = "tavily"
        source_type: SourceType = "web"
        configured = True

        async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
            searches.append(query)
            return SearchResponse(
                provider="tavily",
                source_type="web",
                query=query,
                results=tuple(
                    SearchResult(
                        provider="tavily",
                        source_type="web",
                        title=f"{query} result {i}",
                        url=f"https://example.org/{len(searches)}/{i}",
                        content=f"Public source {query} result {i} supports this topic.",
                    )
                    for i in range(min(3, limit))
                ),
            )

        async def close(self) -> None:
            return None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal evidence_attempts
        payload = json.loads(request.content)
        requests.append(payload)
        name = payload["tools"][0]["function"]["name"]
        if name == "submit_plan":
            arguments = {"mode": "research", "steps": ["Research all five topics"]}
        elif name == "submit_research_queries":
            arguments = {"queries": queries}
        elif name == "submit_evidence":
            evidence_attempts += 1
            numbers = list(range(1, 16)) if evidence_attempts == 1 else selected
            arguments = {
                "items": [
                    {
                        "candidate_id": f"C{i}",
                        "claim": "This source supports the topic",
                        "relevance": 0.9,
                    }
                    for i in numbers
                ]
            }
            if evidence_attempts == 1:
                assert (
                    "maxItems"
                    not in payload["tools"][0]["function"]["parameters"]["properties"]["items"]
                )
                assert "1 to 12 items TOTAL" in payload["messages"][0]["content"]
            else:
                assert "Protocol correction" in payload["messages"][-1]["content"]
                assert (
                    "maxItems at schema /properties/items/maxItems"
                    in payload["messages"][-1]["content"]
                )
        else:
            assert name == "submit_research_report"
            arguments = {
                "answer": " ".join(f"Supported topic [E{i}]." for i in range(1, 11)),
                "citations": [
                    {"claim": "Supported topic", "evidence_ids": [f"E{i}"]} for i in range(1, 11)
                ],
            }
        return httpx.Response(200, json=completion([(name, arguments)]))

    store, engine = await repository()
    settings = configured_settings()
    settings.llm_compatibility_profile = "deepseek"
    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        runner = AgentRunner(
            provider=BudgetedLLM(OpenAICompatibleProvider(settings, http_client=client)),
            repository=store,
            tools=[],
            research_services=[ManyResults()],
        )
        try:
            result = await runner.run(
                "Compare five topics", mode="research", research_sources=["web"]
            )
            assert result.task.status == "succeeded" and len(result.task.evidence) == 10
            assert {e.topic for e in result.task.evidence} == {q["topic"] for q in queries}
            assert len(searches) == budget.used["search_queries"] == 5
            assert evidence_attempts == 2
            assert (
                len(requests)
                == result.task.metrics["model_calls"]
                == budget.used["model_calls"]
                == 5
            )
            assert result.task.metrics["total_tokens"] == budget.used["total_tokens"] == 15
        finally:
            await engine.dispose()
            current_budget.reset(token)
