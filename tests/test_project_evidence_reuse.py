"""Two different goals share original evidence without replaying the first task."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from sqlalchemy import select

from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.budget import BudgetedLLM
from autoscholar.evidence.database import EvidenceReuseRow, ProjectClaimRow, SourceVersionRow
from autoscholar.evidence.library import EvidenceLibrary, EvidenceRevoked
from autoscholar.evidence.models import current_evidence
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.orchestration.durable import DurableService
from tests.test_autonomous import call
from tests.test_coding_schema_feedback import completion
from tests.test_llm_provider import configured_settings
from tests.test_research_quality import QualityProvider, finish, setup
from tests.test_research_workflow import OBJECTIVE, RESEARCH_PLAN


class PrefixProvider(QualityProvider):
    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
        if tools and tools[0].name == "submit_research_quality":
            for section in result.tool_calls[0].arguments["sections"]:
                for claim in section["claims"]:
                    claim["text"] = "LoRA adaptation: " + claim["text"]
        return result


class ReusingProvider(PrefixProvider):
    def __init__(self, objective: str) -> None:
        super().__init__()
        self.objective = objective

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        name = tools[0].name if tools else ""
        if name == "submit_research_quality":
            return await super().generate(messages, tools=tools, tool_choice=tool_choice)
        self.calls.append({"messages": messages, "tools": tools})
        if name == "submit_task_plan":
            plan = copy.deepcopy(RESEARCH_PLAN)
            plan["goal"] = plan["steps"][0]["description"] = self.objective
            return call(name, plan)
        if name == "submit_review":
            return call(name, dict(status="PASS", issues=[], suggested_steps=[]))
        if name == "submit_plan":
            return call(name, dict(mode="research", steps=["Compare prior original sources"]))
        if name == "submit_research_queries":
            bank = current_evidence.get()
            assert bank and bank.claims
            return call(
                name,
                dict(
                    queries=[],
                    reuse=[
                        dict(
                            claim_id=c.id,
                            topic="LoRA adaptation",
                            reason="Relevant original passages",
                        )
                        for c in bank.claims
                    ],
                ),
            )
        if name == "submit_evidence":
            raw = (
                messages[1]
                .content.split("<untrusted_sources>\n", 1)[1]
                .split("\n</untrusted_sources>", 1)[0]
            )
            candidates = json.loads(raw)
            return call(
                name,
                dict(
                    items=[
                        dict(
                            candidate_id=c["candidate_id"],
                            claim="LoRA adaptation evidence",
                            relevance=0.9,
                        )
                        for c in candidates
                    ]
                ),
            )
        if name == "submit_research_report":
            evidence = json.loads(messages[1].content.split("Verified evidence:\n", 1)[1])
            return call(
                name,
                dict(
                    answer=" ".join(f"Supported result [{e['id']}]." for e in evidence),
                    citations=[
                        dict(claim="Supported result", evidence_ids=[e["id"]]) for e in evidence
                    ],
                ),
            )
        raise AssertionError(name)


async def published(tmp_path: Path) -> tuple[Any, Any, EvidenceLibrary, str]:
    service, engine = await setup(tmp_path, PrefixProvider())
    for search in service.runner._research_services.values():
        search.results = {
            key: replace(value, fetched_at=datetime.now(UTC).isoformat())
            for key, value in search.results.items()
        }
    task_id, _ = await DurableService(service).submit(
        dict(
            objective=OBJECTIVE,
            workflow_kind="research",
            project_id="research-project",
            research_depth="standard",
        ),
        "first-goal",
    )
    _, job = await finish(service, task_id)
    assert job.status == "succeeded", job.error_code
    library = EvidenceLibrary(service.tasks.session_factory)
    assert await library.publish_one()
    return service, engine, library, task_id


async def test_second_goal_reuses_original_sources_without_external_search(tmp_path: Path) -> None:
    service, engine, library, first_id = await published(tmp_path)
    try:
        objective = "Compare LoRA adaptation methods"
        provider = ReusingProvider(objective)
        service.provider.provider = provider
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=objective,
                workflow_kind="comparison",
                project_id="research-project",
                research_depth="standard",
                reuse_project_evidence=True,
            ),
            "second-goal",
        )
        snapshot, job = await finish(service, task_id)
        assert job.status == "succeeded", job.error_code
        assert len(snapshot.evidence_context.claims) == 2
        assert job.usage.get("search_queries", 0) == 0
        _, first_job = await DurableService(service).repository.snapshot(first_id)
        assert first_job.usage["search_queries"] == 3
        assert snapshot.quality.accepted and snapshot.answer
        assert "https://" in snapshot.answer
        assert all(m.bank_claim_ids for m in snapshot.quality.materials)
        first = await service.tasks.get_task(first_id)
        assert first and first.status == "succeeded"
        async with library.sessions() as session:
            receipts = list(
                await session.scalars(
                    select(EvidenceReuseRow).where(EvidenceReuseRow.task_id == task_id)
                )
            )
            assert len(receipts) == 2 and all(r.status == "adopted" for r in receipts)
        assert await library.publish_one()
        async with library.sessions() as session:
            assert len(list(await session.scalars(select(SourceVersionRow)))) == 2
            derived = list(
                await session.scalars(
                    select(ProjectClaimRow).where(ProjectClaimRow.task_id == task_id)
                )
            )
            assert all(row.dependencies for row in derived)
        await WorkbenchRepository(library.sessions).delete_task("research-project", first_id)
        async with library.sessions() as session:
            for row in derived:
                assert not await library.valid_claim(session, row, project_id="research-project")
    finally:
        await engine.dispose()


async def test_index_failure_falls_back_to_scoped_keywords_and_cycles_are_rejected(
    tmp_path: Path,
) -> None:
    _, engine, library, _ = await published(tmp_path)

    class UnavailableIndex:
        async def search(self, *args: object, **kwargs: object) -> dict[str, float]:
            raise ConnectionError("fixture unavailable")

    library.semantic = UnavailableIndex()
    try:
        selected = await library.select(
            "research-project", "LoRA adaptation", document_ids=[], source_types=["web", "paper"]
        )
        assert selected.claims and selected.note == "keyword_fallback"
        async with library.sessions() as session:
            rows = list(await session.scalars(select(ProjectClaimRow)))
            rows[0].dependencies, rows[1].dependencies = [rows[1].id], [rows[0].id]
            await session.commit()
        assert not (
            await library.select(
                "research-project",
                "LoRA adaptation",
                document_ids=[],
                source_types=["web", "paper"],
            )
        ).claims
    finally:
        await engine.dispose()


async def test_selection_is_bounded_scoped_and_detects_revocation(tmp_path: Path) -> None:
    _, engine, library, first_id = await published(tmp_path)
    try:
        selected = await library.select(
            "research-project", "LoRA adaptation", document_ids=[], source_types=["web", "paper"]
        )
        assert selected.claims and all(s.fresh for s in selected.sources)
        assert sum(len(s.material.text) for s in selected.sources) <= 24000
        await library.validate(selected, [])
        unrelated = await library.select(
            "research-project", "Ocean chemistry", document_ids=[], source_types=["web"]
        )
        assert not unrelated.claims
        restricted = await library.select(
            "research-project",
            "LoRA adaptation",
            document_ids=[],
            source_types=["web"],
            document_only=True,
        )
        assert not restricted.claims
        await WorkbenchRepository(library.sessions).delete_task("research-project", first_id)
        with pytest.raises(EvidenceRevoked):
            await library.validate(selected, [])
        assert not (
            await library.select(
                "research-project",
                "LoRA adaptation",
                document_ids=[],
                source_types=["web", "paper"],
            )
        ).claims
    finally:
        await engine.dispose()


async def test_stale_acquisition_does_not_become_fresh_when_selected(tmp_path: Path) -> None:
    _, engine, library, _ = await published(tmp_path)
    try:
        old = datetime.now(UTC) - timedelta(days=100)
        async with library.sessions() as session:
            for source in await session.scalars(select(SourceVersionRow)):
                source.fetched_at = old
            await session.commit()
        selected = await library.select(
            "research-project", "LoRA adaptation", document_ids=[], source_types=["web", "paper"]
        )
        assert selected.claims and not any(s.fresh for s in selected.sources)
        assert all(s.fetched_at and s.fetched_at.date() == old.date() for s in selected.sources)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("profile", ["generic", "deepseek"])
async def test_reuse_crosses_real_sdk_codec_with_zero_query_schema(
    tmp_path: Path, profile: Literal["generic", "deepseek"]
) -> None:
    service, engine, _, _ = await published(tmp_path)
    objective = "Compare LoRA adaptation methods"
    responder = ReusingProvider(objective)
    requests: list[dict[str, Any]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        result = await responder.generate(
            [
                ChatMessage(role="user", content=item.get("content") or "")
                for item in payload["messages"]
            ],
            tools=[
                ToolDefinition(name=item["function"]["name"], description="Fixture", parameters={})
                for item in payload["tools"]
            ],
        )
        return httpx.Response(
            200, json=completion([(item.name, item.arguments) for item in result.tool_calls])
        )

    settings = configured_settings()
    settings.llm_compatibility_profile = profile
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        metered = BudgetedLLM(OpenAICompatibleProvider(settings, http_client=client))
        service.provider = service.runner._provider = service.quality.provider = metered
        try:
            task_id, _ = await DurableService(service).submit(
                dict(
                    objective=objective,
                    workflow_kind="comparison",
                    project_id="research-project",
                    research_depth="standard",
                    reuse_project_evidence=True,
                ),
                "sdk-reuse",
            )
            snapshot, job = await finish(service, task_id)
            assert job.status == "succeeded", job.error_code
            assert snapshot.quality.accepted and job.usage.get("search_queries", 0) == 0
            queries = next(
                item
                for item in requests
                if item["tools"][0]["function"]["name"] == "submit_research_queries"
            )
            assert "reuse" in queries["tools"][0]["function"]["parameters"]["required"]
            assert job.usage["model_calls"] == len(requests)
        finally:
            await engine.dispose()
