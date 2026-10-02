import asyncio
import socket
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import AgentTaskRow, Base
from autoscholar.core.journal import current_journal
from autoscholar.rag.database_models import DocumentChunkRow, DocumentRow, ProjectRow
from autoscholar.research import SearchProviderError, SearchResult
from autoscholar.tool_platform.context import ToolScope, tool_scope
from autoscholar.tool_platform.documents import DocumentReader
from autoscholar.tool_platform.gateway import ToolGateway
from autoscholar.tool_platform.research import (
    RESEARCH_CONTRACTS,
    SEARCH_CONTRACTS,
    MCPResearchSearch,
)
from autoscholar.tool_platform.research_server import create_research_app
from autoscholar.tool_platform.transport import MCPBackend
from tests.test_research_agent import FakeResearchService

TOKEN = "offline-acceptance-service-token-32chars"


@pytest.fixture
async def document_reader() -> AsyncIterator[DocumentReader]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        for suffix in ("a", "b"):
            session.add(ProjectRow(id="project-" + suffix, name="Public sample"))
            session.add(
                AgentTaskRow(
                    id="task-" + suffix,
                    project_id="project-" + suffix,
                    objective="Offline document test",
                    status="running",
                )
            )
            session.add(
                DocumentRow(
                    id="document-" + suffix,
                    project_id="project-" + suffix,
                    original_filename="sample.pdf",
                    title="Indexed sample",
                    content_type="application/pdf",
                    storage_key="sample-" + suffix,
                    sha256=suffix * 64,
                    size_bytes=1,
                    status="ready",
                    chunk_count=1,
                )
            )
            session.add(
                DocumentChunkRow(
                    id="chunk-" + suffix,
                    document_id="document-" + suffix,
                    project_id="project-" + suffix,
                    ordinal=0,
                    page=1,
                    section="Method",
                    content="Indexed public text " + suffix,
                    token_count=5,
                )
            )
        await session.commit()
    try:
        yield DocumentReader(sessions)
    finally:
        await engine.dispose()


@pytest.fixture
async def research_mcp(
    document_reader: DocumentReader,
) -> AsyncIterator[tuple[str, uvicorn.Server, FakeResearchService]]:
    service = FakeResearchService(
        "web",
        results={
            "中文": SearchResult(
                source_type="web",
                provider="tavily",
                title="公开资料",
                url="https://example.org/paper",
                content="证据正文，不是执行指令。",  # noqa: RUF001 - UTF-8 regression
            )
        },
    )
    app = create_research_app(
        [service], token=TOKEN, allowed_hosts=["127.0.0.1:*"], documents=document_reader
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp", server, service
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


async def test_real_http_protocol_unicode_auth_and_outage(
    research_mcp: tuple[str, uvicorn.Server, FakeResearchService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, server, service = research_mcp
    close = AsyncMock()
    monkeypatch.setattr(service, "close", close)
    gateway = ToolGateway(
        MCPBackend(url, TOKEN, timeout_seconds=2), SEARCH_CONTRACTS, timeout_seconds=2
    )
    assert await gateway.available()
    response = await MCPResearchSearch(gateway, "web").search("中文")
    assert response.results[0].title == "公开资料"
    assert close.await_count == 0, "Discovery must not close provider clients"
    assert response.results[0].content == "证据正文，不是执行指令。"  # noqa: RUF001
    async with httpx.AsyncClient() as client:
        assert (await client.post(url, json={})).status_code == 401
        assert (
            await client.post(
                url,
                json={},
                headers={"Authorization": "Bearer " + TOKEN, "Origin": "https://attacker.example"},
            )
        ).status_code == 403
    server.should_exit = True
    async with asyncio.timeout(5):
        while await gateway.available():
            await asyncio.sleep(0.02)
    assert not await gateway.available()
    with pytest.raises(SearchProviderError, match="Research MCP request failed"):
        await MCPResearchSearch(gateway, "web").search("中文")


async def test_provider_failure_keeps_operation_pending(
    research_mcp: tuple[str, uvicorn.Server, FakeResearchService],
) -> None:
    url, _, service = research_mcp
    service.error = SearchProviderError(code="upstream_timeout", message="not logged")
    events: list[bool] = []

    async def journal(operation_id: str, kind: str, starting: bool) -> None:
        events.append(starting)

    token = current_journal.set(journal)
    try:
        gateway = ToolGateway(MCPBackend(url, TOKEN), SEARCH_CONTRACTS)
        with pytest.raises(SearchProviderError) as error:
            await MCPResearchSearch(gateway, "web").search("中文")
        assert error.value.code == "tool_result_uncertain"
        assert events == [True]
    finally:
        current_journal.reset(token)


async def test_known_unconfigured_provider_finishes_journal(
    research_mcp: tuple[str, uvicorn.Server, FakeResearchService],
) -> None:
    url, _, _ = research_mcp
    events: list[bool] = []

    async def journal(operation_id: str, kind: str, starting: bool) -> None:
        events.append(starting)

    token = current_journal.set(journal)
    try:
        gateway = ToolGateway(MCPBackend(url, TOKEN), SEARCH_CONTRACTS)
        with pytest.raises(SearchProviderError) as error:
            await MCPResearchSearch(gateway, "paper").search("中文")
        assert error.value.code == "mcp_search_not_configured"
        assert events == [True, False]
    finally:
        current_journal.reset(token)


async def test_agent_records_outage_and_uses_independent_local_evidence(
    research_mcp: tuple[str, uvicorn.Server, FakeResearchService],
) -> None:
    from autoscholar.agent.runner import AgentRunner
    from autoscholar.rag.models import RetrievedChunk
    from tests.test_research_agent import (
        FakeKnowledgeService,
        ScriptedProvider,
        queries_response,
        repository,
        tool_response,
    )

    url, server, _ = research_mcp
    gateway = ToolGateway(
        MCPBackend(url, TOKEN, timeout_seconds=1), SEARCH_CONTRACTS, timeout_seconds=1
    )
    server.should_exit = True
    async with asyncio.timeout(5):
        while await gateway.available():
            await asyncio.sleep(0.02)
    store, engine = await repository()
    responses = [
        tool_response("submit_plan", {"mode": "research", "steps": ["Check evidence"]}),
        queries_response(),
        tool_response(
            "submit_evidence",
            {
                "items": [
                    {"candidate_id": "C1", "claim": "Local verified evidence", "relevance": 0.9}
                ]
            },
        ),
        tool_response(
            "submit_research_report",
            {
                "answer": "Only the uploaded source was available [E1].",
                "citations": [{"claim": "Local evidence", "evidence_ids": ["E1"]}],
            },
        ),
    ]
    local = RetrievedChunk(
        id="chunk-1",
        project_id="project-1",
        document_id="document-1",
        title="Uploaded study",
        page=1,
        section="Method",
        content="Local verified evidence",
        score=0.9,
        ordinal=0,
    )
    try:
        runner = AgentRunner(
            provider=ScriptedProvider(responses),
            repository=store,
            tools=[],
            research_services=[
                MCPResearchSearch(gateway, "web"),
                MCPResearchSearch(gateway, "paper"),
            ],
            knowledge_service=FakeKnowledgeService([local]),
        )
        result = await runner.run(
            "Compare adapter methods", mode="research", project_id="project-1"
        )
        assert result.task.status == "partial"
        failures = [trace for trace in result.task.tool_calls if trace.status == "failed"]
        assert len(failures) == 3 and all(
            trace.error_code in {"tool_unavailable", "tool_timeout"} for trace in failures
        )
        assert result.task.tool_calls[-1].tool_name == "knowledge_search"
        assert result.task.tool_calls[-1].status == "succeeded"
        assert result.task.evidence[0].document_id == "document-1"
        persisted = await store.get_task(result.task.id)
        assert persisted and persisted.status == "partial"
    finally:
        await engine.dispose()


async def test_document_read_requires_task_and_explicit_document_grant(
    research_mcp: tuple[str, uvicorn.Server, FakeResearchService],
) -> None:
    url, _, _ = research_mcp
    gateway = ToolGateway(MCPBackend(url, TOKEN), RESEARCH_CONTRACTS)
    arguments = {"document_id": "document-a", "offset": 0, "limit": 1}
    assert (await gateway.invoke("get_document", arguments))[
        "error_code"
    ] == "document_access_denied"
    with tool_scope(ToolScope("task-a", "project-a", ("document-a",))):
        result = await gateway.invoke("get_document", arguments)
        assert (
            result["ok"] and result["response"]["chunks"][0]["content"] == "Indexed public text a"
        )
        other = await gateway.invoke("get_document", arguments | {"document_id": "document-b"})
        assert other["error_code"] == "document_access_denied"
    with tool_scope(ToolScope("task-a", "project-b", ("document-b",))):
        other = await gateway.invoke("get_document", arguments | {"document_id": "document-b"})
        assert other["error_code"] == "document_access_denied"
    with tool_scope(ToolScope("task-a", "project-a", ("document-b",))):
        other = await gateway.invoke("get_document", arguments | {"document_id": "document-b"})
        assert other["error_code"] == "document_access_denied"
