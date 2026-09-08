from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.agent.runner import AgentRunError, AgentRunner
from autoscholar.llm import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.rag.models import RetrievalMode, RetrievedChunk
from autoscholar.rag.service import RAGQueryResult
from autoscholar.research import SearchProviderError, SearchResponse, SearchResult, SourceType


class ScriptedProvider:
    configured = True

    def __init__(self, responses: list[LLMResult]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


def tool_response(name: str, arguments: dict[str, Any]) -> LLMResult:
    return LLMResult(
        text="",
        model="test-model",
        usage=TokenUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        tool_calls=(ToolCall(id=f"{name}-call", name=name, arguments=arguments),),
    )


class FakeResearchService:
    def __init__(
        self,
        source_type: SourceType,
        *,
        results: dict[str, SearchResult] | None = None,
        error: SearchProviderError | None = None,
        configured: bool = True,
    ) -> None:
        self.source_type = source_type
        self.name = "tavily" if source_type == "web" else "semantic_scholar"
        self.configured = configured
        self.results = results or {}
        self.error = error

    async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        del limit
        if self.error:
            raise self.error
        result = self.results.get(query)
        return SearchResponse(
            provider=self.name,
            source_type=self.source_type,
            query=query,
            results=(result,) if result else (),
        )

    async def close(self) -> None:
        return None


class FakeKnowledgeService:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks

    async def retrieve(
        self,
        question: str,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        retrieval_mode: RetrievalMode = "dense",
        top_k: int | None = None,
    ) -> list[RetrievedChunk]:
        del question, project_id, document_ids, retrieval_mode
        return self.chunks[:top_k]

    async def query(
        self,
        question: str,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        retrieval_mode: RetrievalMode = "dense",
        top_k: int | None = None,
        task_id: str | None = None,
        task_exists: bool = False,
    ) -> RAGQueryResult:
        del (
            question,
            project_id,
            document_ids,
            retrieval_mode,
            top_k,
            task_id,
            task_exists,
        )
        raise NotImplementedError


async def repository() -> tuple[AgentTaskRepository, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False)), engine


def queries_response() -> LLMResult:
    return tool_response(
        "submit_research_queries",
        {
            "queries": [
                {
                    "topic": "LoRA",
                    "query": "LoRA original paper",
                    "source_type": "paper",
                    "purpose": "Find the original method",
                },
                {
                    "topic": "QLoRA",
                    "query": "QLoRA original paper",
                    "source_type": "paper",
                    "purpose": "Find the quantized method",
                },
                {
                    "topic": "DoRA",
                    "query": "DoRA method overview",
                    "source_type": "web",
                    "purpose": "Find an independent overview",
                },
            ]
        },
    )


def search_services() -> list[FakeResearchService]:
    papers = {
        "LoRA original paper": SearchResult(
            source_type="paper",
            provider="semantic_scholar",
            title="LoRA",
            url="https://doi.org/10.1/lora",
            content="LoRA freezes pretrained weights and adds trainable low-rank matrices.",
            authors=("Alice",),
            year=2021,
            external_id="10.1/lora",
        ),
        "QLoRA original paper": SearchResult(
            source_type="paper",
            provider="semantic_scholar",
            title="QLoRA",
            url="https://doi.org/10.1/qlora",
            content="QLoRA backpropagates through a frozen 4-bit quantized model into adapters.",
            authors=("Bob",),
            year=2023,
            external_id="10.1/qlora",
        ),
    }
    web = {
        "DoRA method overview": SearchResult(
            source_type="web",
            provider="tavily",
            title="DoRA overview",
            url="https://example.com/dora",
            content="DoRA decomposes weights into magnitude and direction for adaptation.",
        )
    }
    return [
        FakeResearchService("web", results=web),
        FakeResearchService("paper", results=papers),
    ]


def successful_responses() -> list[LLMResult]:
    return [
        tool_response(
            "submit_plan",
            {"mode": "compute", "steps": ["Find papers", "Compare methods"]},
        ),
        queries_response(),
        tool_response(
            "submit_evidence",
            {
                "items": [
                    {
                        "candidate_id": "C1",
                        "claim": "LoRA adds trainable low-rank matrices",
                        "excerpt": (
                            "LoRA freezes pretrained weights and adds trainable low-rank matrices."
                        ),
                        "relevance": 0.98,
                    },
                    {
                        "candidate_id": "C2",
                        "claim": "QLoRA trains adapters through a frozen 4-bit model",
                        "excerpt": (
                            "QLoRA backpropagates through a frozen 4-bit quantized model into "
                            "adapters."
                        ),
                        "relevance": 0.97,
                    },
                    {
                        "candidate_id": "C3",
                        "claim": "DoRA separates magnitude and direction",
                        "excerpt": (
                            "DoRA decomposes weights into magnitude and direction for adaptation."
                        ),
                        "relevance": 0.96,
                    },
                ]
            },
        ),
        tool_response(
            "submit_research_report",
            {
                "answer": "LoRA uses low-rank matrices [E1]. QLoRA adds 4-bit quantization [E2]. "
                "DoRA separates magnitude and direction [E3].",
                "citations": [
                    {"claim": "LoRA mechanism", "evidence_ids": ["E1"]},
                    {"claim": "QLoRA mechanism", "evidence_ids": ["E2"]},
                    {"claim": "DoRA mechanism", "evidence_ids": ["E3"]},
                ],
            },
        ),
    ]


async def test_research_graph_searches_extracts_and_cites_verified_evidence() -> None:
    store, engine = await repository()
    provider = ScriptedProvider(successful_responses())
    runner = AgentRunner(
        provider=provider,
        repository=store,
        tools=[],
        research_services=search_services(),
    )

    result = await runner.run("Compare LoRA, QLoRA and DoRA", mode="research")

    assert result.task.status == "succeeded"
    assert result.task.mode == "research"
    assert [item.citation_key for item in result.task.evidence] == ["E1", "E2", "E3"]
    assert [trace.tool_name for trace in result.task.tool_calls] == [
        "paper_search",
        "paper_search",
        "web_search",
    ]
    assert result.task.citations[2].evidence_ids == ("E3",)
    assert result.task.metrics["model_calls"] == 4
    assert result.task.metrics["iterations"] == 3
    extractor_prompt = provider.calls[2]["messages"][0]
    assert "UNTRUSTED DATA" in extractor_prompt.content
    await engine.dispose()


async def test_research_with_project_mixes_local_and_external_evidence() -> None:
    store, engine = await repository()
    responses = successful_responses()
    responses[2] = tool_response(
        "submit_evidence",
        {
            "items": [
                {
                    "candidate_id": "C1",
                    "claim": "LoRA adds trainable low-rank matrices",
                    "excerpt": (
                        "LoRA freezes pretrained weights and adds trainable low-rank matrices."
                    ),
                    "relevance": 0.98,
                },
                {
                    "candidate_id": "C2",
                    "claim": "QLoRA trains adapters through a frozen 4-bit model",
                    "excerpt": (
                        "QLoRA backpropagates through a frozen 4-bit quantized model into "
                        "adapters."
                    ),
                    "relevance": 0.97,
                },
                {
                    "candidate_id": "C3",
                    "claim": "DoRA separates magnitude and direction",
                    "excerpt": (
                        "DoRA decomposes weights into magnitude and direction for adaptation."
                    ),
                    "relevance": 0.96,
                },
                {
                    "candidate_id": "C4",
                    "claim": "The uploaded paper reports an adapter rank of eight",
                    "excerpt": "We use rank eight for all adapter layers.",
                    "relevance": 0.94,
                },
            ]
        },
    )
    responses[3] = tool_response(
        "submit_research_report",
        {
            "answer": (
                "LoRA uses low-rank matrices [E1], QLoRA uses 4-bit weights [E2], "
                "DoRA separates magnitude and direction [E3], and the uploaded paper uses "
                "rank eight [E4]."
            ),
            "citations": [
                {"claim": "LoRA", "evidence_ids": ["E1"]},
                {"claim": "QLoRA", "evidence_ids": ["E2"]},
                {"claim": "DoRA", "evidence_ids": ["E3"]},
                {"claim": "Local rank", "evidence_ids": ["E4"]},
            ],
        },
    )
    local = RetrievedChunk(
        id="chunk-local",
        project_id="project-1",
        document_id="document-1",
        title="Uploaded adapter study",
        page=7,
        section="Experiments",
        content="We use rank eight for all adapter layers.",
        score=0.94,
        ordinal=12,
    )
    runner = AgentRunner(
        provider=ScriptedProvider(responses),
        repository=store,
        tools=[],
        research_services=search_services(),
        knowledge_service=FakeKnowledgeService([local]),
    )

    result = await runner.run(
        "Compare adapter methods with my uploaded study",
        mode="research",
        project_id="project-1",
    )

    assert result.task.status == "succeeded"
    assert [trace.tool_name for trace in result.task.tool_calls][-1] == "knowledge_search"
    local_evidence = result.task.evidence[-1]
    assert local_evidence.source_type == "document"
    assert local_evidence.document_id == "document-1"
    assert local_evidence.chunk_id == "chunk-local"
    assert local_evidence.page == 7
    await engine.dispose()


async def test_research_rejects_fabricated_excerpt_and_marks_partial() -> None:
    store, engine = await repository()
    responses = successful_responses()
    responses[2] = tool_response(
        "submit_evidence",
        {
            "items": [
                {
                    "candidate_id": "C1",
                    "claim": "Fabricated claim",
                    "excerpt": "This text is not in the source.",
                    "relevance": 1.0,
                },
                {
                    "candidate_id": "C3",
                    "claim": "DoRA separates magnitude and direction",
                    "excerpt": (
                        "DoRA decomposes weights into magnitude and direction for adaptation."
                    ),
                    "relevance": 0.9,
                },
            ]
        },
    )
    responses[3] = tool_response(
        "submit_research_report",
        {
            "answer": "Available evidence describes DoRA [E1].",
            "citations": [{"claim": "DoRA mechanism", "evidence_ids": ["E1"]}],
        },
    )
    runner = AgentRunner(
        provider=ScriptedProvider(responses),
        repository=store,
        tools=[],
        research_services=search_services(),
    )

    result = await runner.run("Compare methods", mode="research")

    assert result.task.status == "partial"
    assert len(result.task.evidence) == 1
    assert {warning.code for warning in result.task.warnings} == {
        "evidence_items_rejected",
        "research_coverage_incomplete",
    }
    await engine.dispose()


async def test_single_provider_failure_returns_partial_with_failed_trace() -> None:
    store, engine = await repository()
    responses = successful_responses()
    responses[2] = tool_response(
        "submit_evidence",
        {
            "items": [
                {
                    "candidate_id": "C1",
                    "claim": "LoRA adds low-rank matrices",
                    "excerpt": (
                        "LoRA freezes pretrained weights and adds trainable low-rank matrices."
                    ),
                    "relevance": 0.9,
                }
            ]
        },
    )
    responses[3] = tool_response(
        "submit_research_report",
        {
            "answer": "LoRA uses low-rank matrices [E1].",
            "citations": [{"claim": "LoRA mechanism", "evidence_ids": ["E1"]}],
        },
    )
    services = search_services()
    services[0].error = SearchProviderError(
        code="tavily_upstream_error",
        message="Tavily search returned HTTP 503",
        retryable=True,
    )
    runner = AgentRunner(
        provider=ScriptedProvider(responses),
        repository=store,
        tools=[],
        research_services=services,
    )

    result = await runner.run("Compare methods", mode="research")

    assert result.task.status == "partial"
    assert result.task.tool_calls[-1].status == "failed"
    assert result.task.tool_calls[-1].error_code == "tavily_upstream_error"
    assert any(warning.provider == "tavily" for warning in result.task.warnings)
    await engine.dispose()


async def test_no_search_evidence_fails_and_persists_research_status() -> None:
    store, engine = await repository()
    failure = SearchProviderError(code="search_down", message="Search unavailable")
    runner = AgentRunner(
        provider=ScriptedProvider(
            [
                tool_response("submit_plan", {"mode": "research", "steps": ["Search"]}),
                queries_response(),
            ]
        ),
        repository=store,
        tools=[],
        research_services=[
            FakeResearchService("web", error=failure),
            FakeResearchService("paper", error=failure),
        ],
    )

    with pytest.raises(AgentRunError) as exc_info:
        await runner.run("Compare methods", task_id="failed-research", mode="research")

    assert exc_info.value.code == "research_evidence_unavailable"
    persisted = await store.get_task("failed-research")
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.mode == "research"
    assert len(persisted.tool_calls) == 3
    await engine.dispose()


async def test_invalid_citations_are_retried_once() -> None:
    store, engine = await repository()
    responses = successful_responses()
    responses.insert(
        3,
        tool_response(
            "submit_research_report",
            {
                "answer": "Unsupported citation [E99].",
                "citations": [{"claim": "Unsupported", "evidence_ids": ["E99"]}],
            },
        ),
    )
    provider = ScriptedProvider(responses)
    runner = AgentRunner(
        provider=provider,
        repository=store,
        tools=[],
        research_services=search_services(),
    )

    result = await runner.run("Compare methods", mode="research")

    assert result.task.status == "succeeded"
    assert result.task.metrics["model_calls"] == 5
    assert "Correct the invalid citation response" in provider.calls[4]["messages"][-1].content
    await engine.dispose()


async def test_auto_routed_research_failure_keeps_plan_and_mode() -> None:
    store, engine = await repository()
    runner = AgentRunner(
        provider=ScriptedProvider(
            [
                tool_response(
                    "submit_plan",
                    {"mode": "research", "steps": ["Find primary sources"]},
                ),
                LLMResult(text="unstructured queries", model="test-model"),
            ]
        ),
        repository=store,
        tools=[],
        research_services=search_services(),
    )

    with pytest.raises(AgentRunError) as exc_info:
        await runner.run("Research LoRA", task_id="auto-research")

    assert exc_info.value.code == "invalid_research_queries"
    persisted = await store.get_task("auto-research")
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.mode == "research"
    assert persisted.plan == ["Find primary sources"]
    assert persisted.metrics["model_calls"] == 1
    await engine.dispose()
