"""No paid APIs: durable multi-pass research, adversarial citations and extraction."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from autoscholar.api.workbench_quality import quality_view
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.api.workbench_resources import ResourceRepository
from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from autoscholar.orchestration.checkpoints import Snapshot
from autoscholar.orchestration.durable import DurableService
from autoscholar.rag.database_models import DocumentChunkRow, DocumentRow
from autoscholar.research.materials import MaterialReader, public_url
from autoscholar.research.models import SearchResponse, SearchResult
from autoscholar.research.quality import ResearchQuality, identity
from autoscholar.research.quality_models import QualityResponse
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import call
from tests.test_research_workflow import OBJECTIVE, research_script, research_service


class QualityProvider(ScriptedProvider):
    def __init__(self, *, revise: bool = False, gap: bool = False, interrupt: bool = False) -> None:
        super().__init__(research_script()[:-1])
        self.revise, self.gap, self.interrupt = revise, gap, interrupt
        self.checks = 0

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        if not tools or tools[0].name != "submit_research_quality":
            return await super().generate(messages, tools=tools, tool_choice=tool_choice)
        self.calls.append({"messages": messages, "tools": tools})
        if self.interrupt:
            raise httpx.ReadTimeout("fixture interruption")
        materials = json.loads(messages[1].content)["materials"]
        command = messages[-1].content
        result: dict[str, Any] = dict(assessments=[], sections=[], issues=[], decision="continue")
        if command.startswith(("STAGE screen", "STAGE refine")):
            result["assessments"] = [
                dict(
                    source_id=item["id"],
                    verdict="keep",
                    relevance=0.9,
                    time_fit="within_scope" if item["published_at"] else "unknown",
                    reason="Independent source assessment",
                    quote=item["text"][:200],
                )
                for item in materials
            ]
        elif command.startswith("STAGE write"):
            revised = "STAGE revise" in command or (self.gap and self.checks > 0)
            result["sections"] = [
                dict(
                    heading="Evidence synthesis",
                    claims=[
                        dict(
                            text="Corrected supported finding"
                            if revised
                            else "Initial supported finding",
                            citations=[
                                dict(source_id=materials[-1]["id"], quote=materials[-1]["text"])
                            ],
                        ),
                        dict(
                            text="Preserved supported finding",
                            citations=[
                                dict(source_id=materials[0]["id"], quote=materials[0]["text"])
                            ],
                        ),
                    ],
                )
            ]
        else:
            self.checks += 1
            result["decision"] = "accept"
            if (self.revise or self.gap) and self.checks == 1:
                result.update(
                    decision="revise",
                    issues=[
                        dict(
                            draft_quote="Initial supported finding",
                            reason="Needs more precise support",
                            action="search" if self.gap else "revise",
                            search_query="targeted evidence" if self.gap else None,
                            source_type="web",
                        )
                    ],
                )
        return call("submit_research_quality", result)


async def setup(tmp_path: Path, provider: QualityProvider) -> tuple[Any, Any]:
    service, engine = await research_service(tmp_path, provider)
    for search in service.runner._research_services.values():
        search.results = {
            key: replace(value, published_at="2026-10-12") for key, value in search.results.items()
        }
    service.quality = ResearchQuality(
        service.provider, service.tasks, searches=list(service.runner._research_services.values())
    )
    return service, engine


async def finish(service: Any, task_id: str) -> tuple[Snapshot, Any]:
    for _ in range(65):
        # A fresh worker per checkpoint proves completed model passes are not replayed.
        if not await DurableService(service).tick(task_id):
            break
    return await DurableService(service).repository.snapshot(task_id)


@pytest.mark.parametrize("mode", ["standard", "deep"])
@pytest.mark.parametrize("repair", ["none", "revise", "supplement"])
async def test_quality_pipeline_restarts_and_publishes_bound_references(
    tmp_path: Path,
    mode: str,
    repair: str,
) -> None:
    provider = QualityProvider(revise=repair == "revise", gap=repair == "supplement")
    service, engine = await setup(tmp_path, provider)
    try:
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=OBJECTIVE,
                workflow_kind="research",
                project_id="research-project",
                research_depth=mode,
            ),
            "quality",
        )
        snapshot, job = await finish(service, task_id)
        assert job.status == "succeeded", (job.error_code, snapshot.quality)
        assert snapshot.quality.accepted and snapshot.quality.phase == "done"
        assert snapshot.quality.rounds == (repair != "none")
        assert snapshot.quality.supplemental_queries == (repair == "supplement")
        assert job.usage.get("training_runs", 0) == 0
        assert snapshot.answer
        if repair != "none":
            assert "Corrected supported finding" in snapshot.answer
        quality_calls = [
            c for c in provider.calls if c["tools"][0].name == "submit_research_quality"
        ]
        assert len(quality_calls) == (4 if repair == "none" else 7 if repair == "supplement" else 6)
        assert all(c["tools"] == quality_calls[0]["tools"] for c in quality_calls)
        assert quality_calls[1]["messages"][:2] == quality_calls[2]["messages"][:2]
        assert "Independent source assessment" not in quality_calls[1]["messages"][-1].content
        repository = WorkbenchRepository(service.tasks.session_factory)
        resources = await ResourceRepository(repository).page(
            task_id, "evidence", limit=50, offset=0
        )
        assert {r["citation_key"] for r in resources.items} == {"E1", "E3"}
        assert all(r["task_id"] == task_id for r in resources.items)
        await service.quality.persist_evidence(snapshot.restore(job.usage, job.active_seconds))
        assert (await repository.overview(task_id, limit=20, offset=0)).resources.evidence == 2
        audit = await quality_view(repository, task_id)
        assert audit["accepted"] and len(audit["sources"]) == 3
        assert len(audit["reviews"]) == (1 if repair == "none" else 2)
        assert "bundle" not in json.dumps(audit)
    finally:
        await engine.dispose()


async def test_collector_keeps_all_candidates_not_only_child_selection(tmp_path: Path) -> None:
    provider = QualityProvider()
    service, engine = await setup(tmp_path, provider)
    search = service.runner._research_services["web"]
    original = search.search

    async def expanded(query: str, *, limit: int = 5) -> SearchResponse:
        response = await original(query, limit=limit)
        extras = tuple(
            SearchResult(
                source_type="web",
                provider="fixture",
                title=f"Candidate {i}",
                url=f"https://example.org/{i}",
                content=f"Additional independently relevant evidence {i}.",
                published_at="2026-10-15",
            )
            for i in range(4)
        )
        return SearchResponse(
            provider=response.provider,
            source_type="web",
            query=query,
            results=(*response.results, *extras),
        )

    search.search = expanded
    try:
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=OBJECTIVE,
                workflow_kind="research",
                project_id="research-project",
                research_depth="standard",
            ),
            "all-candidates",
        )
        snapshot, job = await finish(service, task_id)
        assert job.status == "succeeded", job.error_code
        assert len(snapshot.quality.materials) == 7
        assert len(snapshot.quality.initial) == len(snapshot.quality.assessments) == 7
    finally:
        await engine.dispose()


async def test_unknown_interrupted_quality_call_is_not_replayed(tmp_path: Path) -> None:
    provider = QualityProvider(interrupt=True)
    service, engine = await setup(tmp_path, provider)
    try:
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=OBJECTIVE,
                workflow_kind="research",
                research_depth="standard",
                project_id="research-project",
            ),
            "interrupted",
        )
        _, job = await finish(service, task_id)
        assert job.pending_calls and job.status == "recovery_required"
        count = len(provider.calls)
        assert not await DurableService(service).tick(task_id)
        assert len(provider.calls) == count
    finally:
        await engine.dispose()


async def test_reserved_review_budget_stops_before_extra_model_calls(tmp_path: Path) -> None:
    provider = QualityProvider()
    service, engine = await setup(tmp_path, provider)
    try:
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=OBJECTIVE,
                workflow_kind="research",
                research_depth="standard",
                project_id="research-project",
                budget={"model_calls": 8},
            ),
            "limited",
        )
        snapshot, job = await finish(service, task_id)
        assert job.status == "budget_exceeded"
        assert len(provider.calls) == 5
        assert snapshot.quality.phase == "screen"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "attack", ["unknown_date", "future_date", "foreign_quote", "missing_source"]
)
async def test_quality_rejects_forged_citations_and_dates(tmp_path: Path, attack: str) -> None:
    service, engine = await setup(tmp_path, QualityProvider())
    try:
        run = Snapshot(
            task_id="fixture", objective=OBJECTIVE, as_of_date=date(2026, 11, 7)
        ).restore({}, 0)
        ResearchQuality._add(
            run.quality,
            SearchResult(
                source_type="web",
                provider="fixture",
                title="Study",
                url="https://example.org/study",
                content="Source evidence of a particular study.",
                published_at="2026-10-12",
            ),
            "topic",
        )
        if attack == "unknown_date":
            run.quality.materials[0].published_at = None
        if attack == "future_date":
            run.quality.materials[0].published_at = "2026-12-01"
        response = QualityResponse.model_validate(
            dict(
                assessments=[
                    dict(
                        source_id="E99" if attack == "missing_source" else "E1",
                        verdict="keep",
                        relevance=0.9,
                        time_fit="within_scope",
                        reason="Fixture",
                        quote="Invented unsupported statement"
                        if attack == "foreign_quote"
                        else "Source evidence of a particular study.",
                    )
                ],
                sections=[],
                issues=[],
                decision="continue",
            )
        )
        with pytest.raises(ValueError):
            service.quality._validate(run, response, "refine")
    finally:
        await engine.dispose()


def test_deduplication_and_old_snapshot_contract() -> None:
    def source(url: str) -> SearchResult:
        return SearchResult(
            source_type="paper", provider="fixture", title="Study", url=url, content="A study"
        )

    assert identity(source("https://arxiv.org/abs/2601.12345v2")) == identity(
        source("https://arxiv.org/pdf/2601.12345v1")
    )
    assert identity(source("https://doi.org/10.1234/ABC")) == identity(
        source("https://publisher.example/10.1234/abc?utm_source=test")
    )
    assert Snapshot(task_id="old", objective="Old fixture").research_depth == "quick"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://localhost/a",
        "file:///a",
        "http://[::1]/",
        "https://foo.internal/a",
        "https://u:p@a.com/",
    ],
)
def test_extraction_cannot_fetch_private_or_credential_urls(url: str) -> None:
    assert not public_url(url)


@pytest.mark.parametrize("mode,limit", [("standard", 1), ("deep", 2)])
async def test_review_failure_is_bounded_and_retains_unverified_draft(
    tmp_path: Path,
    mode: str,
    limit: int,
) -> None:
    class NeverAccepts(QualityProvider):
        async def generate(
            self,
            messages: list[ConversationMessage],
            *,
            tools: list[ToolDefinition] | None = None,
            tool_choice: ToolChoice = "none",
        ) -> LLMResult:
            response = await super().generate(messages, tools=tools, tool_choice=tool_choice)
            if messages[-1].content.startswith("STAGE check"):
                draft = json.loads(messages[-1].content.split("Draft: ", 1)[1])
                response.tool_calls[0].arguments.update(
                    decision="revise",
                    issues=[
                        dict(
                            draft_quote=draft[0]["claims"][0]["text"],
                            reason="Unresolved source conflict",
                            action="revise",
                            search_query=None,
                            source_type="web",
                        )
                    ],
                )
            return response

    service, engine = await setup(tmp_path, NeverAccepts())
    try:
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=OBJECTIVE,
                workflow_kind="research",
                project_id="research-project",
                research_depth=mode,
            ),
            "never-accepts",
        )
        snapshot, job = await finish(service, task_id)
        assert job.status == "failed" and job.error_code == "research_quality_unverified"
        assert snapshot.quality.rounds == limit
        assert len(snapshot.quality.review_history) == limit + 1
        task = await service.tasks.get_task(task_id)
        assert task and "尚未通过" in (task.answer or "")
        assert not snapshot.quality.accepted
    finally:
        await engine.dispose()


async def test_independent_rescreen_can_recover_rejected_material(tmp_path: Path) -> None:
    class Reconsiders(QualityProvider):
        async def generate(
            self,
            messages: list[ConversationMessage],
            *,
            tools: list[ToolDefinition] | None = None,
            tool_choice: ToolChoice = "none",
        ) -> LLMResult:
            result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
            if messages[-1].content.startswith("STAGE screen"):
                result.tool_calls[0].arguments["assessments"][-1].update(
                    verdict="reject", quote="", reason="Initial false negative"
                )
            return result

    service, engine = await setup(tmp_path, Reconsiders())
    try:
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=OBJECTIVE,
                workflow_kind="research",
                project_id="research-project",
                research_depth="standard",
            ),
            "reconsider",
        )
        snapshot, job = await finish(service, task_id)
        assert job.status == "succeeded"
        assert snapshot.quality.initial[-1].verdict == "reject"
        assert snapshot.quality.assessments[-1].verdict == "keep"
        assert "[E3]" in (snapshot.answer or "")
    finally:
        await engine.dispose()


async def test_document_reading_stays_in_selected_project_and_snapshot(tmp_path: Path) -> None:
    service, engine = await setup(tmp_path, QualityProvider())
    try:
        async with service.tasks.session_factory() as session:
            session.add(
                DocumentRow(
                    id="doc",
                    project_id="research-project",
                    title="Local study",
                    original_filename="study.pdf",
                    content_type="application/pdf",
                    sha256="a" * 64,
                    storage_key="not-read-from-disk",
                    size_bytes=100,
                    status="ready",
                )
            )
            await session.flush()
            for ordinal, content in enumerate(
                ["Previous context.", "Selected document evidence.", "Following context."]
            ):
                session.add(
                    DocumentChunkRow(
                        id=f"chunk-{ordinal}",
                        document_id="doc",
                        project_id="research-project",
                        ordinal=ordinal,
                        page=1,
                        content=content,
                        token_count=10,
                    )
                )
            await session.commit()
        run = Snapshot(
            task_id="docs",
            objective="Analyse selected documents",
            project_id="research-project",
            document_ids=["doc"],
            workflow_kind="document_analysis",
        ).restore({}, 0)
        ResearchQuality._add(
            run.quality,
            SearchResult(
                source_type="document",
                provider="fixture",
                title="Local study",
                url="document:doc",
                content="Selected document evidence.",
                document_id="doc",
                chunk_id="chunk-1",
                page=1,
            ),
            "topic",
        )
        source = run.quality.materials[0]
        await service.quality._document(run, source)
        assert "Previous context." in source.text and "Following context." in source.text
        source.text = "Selected document evidence."
        run.document_ids = ["different-doc"]
        await service.quality._document(run, source)
        assert source.text == "Selected document evidence."
        run.document_ids = ["doc"]
        run.project_id = "different-project"
        await service.quality._document(run, source)
        assert source.text == "Selected document evidence."
    finally:
        await engine.dispose()


class Cache:
    value: dict[str, Any] | None = None

    async def get_json(self, key: str) -> Any | None:
        return self.value

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int) -> None:
        self.value = value


@pytest.mark.parametrize("failure", [False, True])
async def test_extraction_partial_failure_truncation_and_cache(failure: bool) -> None:
    count = 0

    def response(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json=dict(
                results=[]
                if failure
                else [
                    dict(
                        url="https://example.org/study",
                        raw_content="x" * 13000,
                    )
                ],
                failed_results=[dict(url="https://example.org/other", error="failed")],
            ),
        )

    reader = MaterialReader(
        api_key="fixture",
        cache=Cache(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
    try:
        result = await reader.read("https://example.org/study")
        assert bool(result.error) == failure
        if not failure:
            assert len(result.text) == 12000 and result.truncated
            cached = await reader.read("https://example.org/study")
            assert cached.cache_hit and count == 1
        assert budget.used == {"tool_calls": 1, "search_queries": 1}
    finally:
        current_budget.reset(token)
        await reader.close()
