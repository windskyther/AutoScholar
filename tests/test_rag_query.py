from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.records import AgentTaskRecord, CitationRecord, EvidenceRecord
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.llm import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.main import create_app
from autoscholar.rag.models import DocumentChunkRecord, DocumentRecord, RetrievedChunk
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.service import RAGQueryResult, RAGQueryService
from tests.test_chat import SuccessfulProvider
from tests.test_health import FakeDependency


class FakeEmbeddingProvider:
    model = "test-embedding"
    dimensions = 2

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in documents]

    async def embed_query(self, query: str) -> list[float]:
        assert query
        return [1.0, 0.0]

    async def close(self) -> None:
        return None


class FakeChunkIndex:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks
        self.searches: list[dict[str, Any]] = []

    async def ensure_collection(self) -> None:
        return None

    async def replace_document(
        self,
        document: DocumentRecord,
        chunks: Sequence[DocumentChunkRecord],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        del document, chunks, vectors

    async def delete_document(self, project_id: str, document_id: str) -> None:
        del project_id, document_id

    async def search_dense(
        self,
        *,
        project_id: str,
        vector: Sequence[float],
        document_ids: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[RetrievedChunk]:
        self.searches.append(
            {
                "project_id": project_id,
                "vector": list(vector),
                "document_ids": list(document_ids) if document_ids is not None else None,
                "limit": limit,
            }
        )
        return self.chunks[:limit]


class CitedAnswerProvider:
    configured = True

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        assert "UNTRUSTED DATA" in messages[0].content
        assert tools is not None
        assert tool_choice == "auto"
        return LLMResult(
            text="",
            model="test-model",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            tool_calls=(
                ToolCall(
                    id="answer-1",
                    name="submit_knowledge_answer",
                    arguments={
                        "answer": "LoRA freezes base weights and learns adapters [E1].",
                        "citations": [
                            {
                                "claim": "LoRA learns adapters",
                                "evidence_ids": ["E1"],
                            }
                        ],
                    },
                ),
            ),
        )

    async def close(self) -> None:
        return None


async def _ready_knowledge_base() -> tuple[KnowledgeRepository, AgentTaskRepository, Any, str, str]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    knowledge = KnowledgeRepository(sessions)
    tasks = AgentTaskRepository(sessions)
    project = await knowledge.create_project(name="Adapters", description=None)
    document = await knowledge.create_document(
        document_id="document-1",
        project_id=project.id,
        original_filename="lora.pdf",
        title="LoRA",
        content_type="application/pdf",
        storage_key="document-1.pdf",
        sha256="a" * 64,
        size_bytes=100,
    )
    chunk = DocumentChunkRecord(
        id="chunk-1",
        document_id=document.id,
        project_id=project.id,
        ordinal=0,
        page=4,
        section="Method",
        content="The base weights are frozen and trainable low-rank matrices are added.",
        token_count=12,
        created_at=datetime.now(UTC),
    )
    await knowledge.replace_chunks(
        document.id,
        [chunk],
        page_count=10,
        embedding_model="test-embedding",
        index_version=1,
    )
    return knowledge, tasks, engine, project.id, document.id


async def test_dense_rag_query_persists_page_grounded_evidence() -> None:
    knowledge, tasks, engine, project_id, document_id = await _ready_knowledge_base()
    index = FakeChunkIndex(
        [
            RetrievedChunk(
                id="chunk-1",
                project_id=project_id,
                document_id=document_id,
                title="LoRA",
                page=4,
                section="Method",
                content="The base weights are frozen and trainable low-rank matrices are added.",
                score=0.91,
                ordinal=0,
            )
        ]
    )
    service = RAGQueryService(
        provider=CitedAnswerProvider(),
        embeddings=FakeEmbeddingProvider(),
        index=index,
        knowledge=knowledge,
        tasks=tasks,
    )

    result = await service.query(
        "What is the LoRA method?",
        project_id=project_id,
        document_ids=[document_id],
        task_id="rag-task-1",
    )

    assert result.task.status == "succeeded"
    assert result.task.mode == "knowledge"
    assert result.task.project_id == project_id
    assert result.task.answer == "LoRA freezes base weights and learns adapters [E1]."
    assert result.task.citations == [
        CitationRecord(claim="LoRA learns adapters", evidence_ids=("E1",))
    ]
    evidence = result.task.evidence[0]
    assert evidence.source_type == "document"
    assert evidence.document_id == document_id
    assert evidence.chunk_id == "chunk-1"
    assert evidence.page == 4
    assert evidence.section == "Method"
    assert index.searches[0]["document_ids"] == [document_id]
    persisted = await tasks.get_task("rag-task-1")
    assert persisted == result.task
    await engine.dispose()


async def test_rag_query_rejects_non_ready_selected_document() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    knowledge = KnowledgeRepository(sessions)
    project = await knowledge.create_project(name="Queued", description=None)
    document = await knowledge.create_document(
        document_id="queued-document",
        project_id=project.id,
        original_filename="queued.pdf",
        title="Queued",
        content_type="application/pdf",
        storage_key="queued.pdf",
        sha256="b" * 64,
        size_bytes=100,
    )
    service = RAGQueryService(
        provider=CitedAnswerProvider(),
        embeddings=FakeEmbeddingProvider(),
        index=FakeChunkIndex([]),
        knowledge=knowledge,
        tasks=AgentTaskRepository(sessions),
    )

    with pytest.raises(AppError) as exc_info:
        await service.query("Question", project_id=project.id, document_ids=[document.id])

    assert exc_info.value.code == "document_not_ready"
    assert exc_info.value.status_code == 409
    await engine.dispose()


class FakeRAGService:
    async def query(self, *args: object, **kwargs: object) -> RAGQueryResult:
        del args, kwargs
        now = datetime.now(UTC)
        evidence = EvidenceRecord(
            id="evidence-1",
            task_id="rag-task",
            citation_key="E1",
            source_type="document",
            provider="qdrant",
            title="LoRA",
            url="/projects/project-1/documents/document-1/content#page=4",
            authors=(),
            year=None,
            external_id=None,
            query="What is LoRA?",
            topic="Local knowledge base",
            claim="Relevant passage",
            excerpt="Low-rank adapters are trained.",
            relevance=0.9,
            created_at=now,
            document_id="document-1",
            chunk_id="chunk-1",
            page=4,
            section="Method",
        )
        task = AgentTaskRecord(
            id="rag-task",
            status="succeeded",
            objective="What is LoRA?",
            plan=["Retrieve", "Answer"],
            answer="Low-rank adapters are trained [E1].",
            metrics={},
            created_at=now,
            updated_at=now,
            mode="knowledge",
            citations=[CitationRecord(claim="Adapters", evidence_ids=("E1",))],
            evidence=[evidence],
            project_id="project-1",
        )
        return RAGQueryResult(task=task, model="test-model", retrieval_mode="dense")


def test_rag_query_api_exposes_document_page_and_citations() -> None:
    client = TestClient(
        create_app(
            Settings(llm_api_key=None, llm_model=None),
            database=FakeDependency(),
            redis=FakeDependency(),
            qdrant=FakeDependency(),
            llm_provider=SuccessfulProvider(),
            rag_query_service=FakeRAGService(),
        )
    )
    with client:
        response = client.post(
            "/projects/project-1/rag/query",
            json={"question": "  What is LoRA?  ", "retrieval_mode": "dense"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["retrieval_mode"] == "dense"
    assert payload["evidence"][0]["document_id"] == "document-1"
    assert payload["evidence"][0]["page"] == 4
    assert payload["citations"][0]["evidence_ids"] == ["E1"]
