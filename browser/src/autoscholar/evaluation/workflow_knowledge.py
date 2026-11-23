"""One real local public document required by project-scoped Research, never a fake retriever."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from qdrant_client import AsyncQdrantClient

from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.rag_adapter import FixtureChunk
from autoscholar.evaluation.retrieval_ablation import (
    LexicalDense,
    LexicalReranker,
    LexicalSparse,
    NoGeneration,
    RetrievalFixture,
    Vocabulary,
)
from autoscholar.evaluation.workflow_runtime import LocalWorkflow
from autoscholar.rag.database_models import DocumentRow
from autoscholar.rag.index import QdrantChunkIndex
from autoscholar.rag.models import DocumentChunkRecord
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.rag.service import RAGQueryService

PUBLIC_KNOWLEDGE = (
    "The public project uses CPU MNIST subsets for engineering checks. "
    "Saved source, measured metrics and checkpoints must agree; "
    "project memory cannot override the current experiment specification."
)


@asynccontextmanager
async def workflow_knowledge(
    state: LocalWorkflow, project_id: str
) -> AsyncIterator[RAGQueryService]:
    fixture = RetrievalFixture(
        schema_version=1,
        suite_id="public-workflow-knowledge-v1",
        chunks=[FixtureChunk(id="scope", document_id="public-project", text=PUBLIC_KNOWLEDGE)],
        queries={"scope": PUBLIC_KNOWLEDGE},
    )
    vocabulary = Vocabulary(fixture)
    dense, sparse = LexicalDense(vocabulary), LexicalSparse(vocabulary)
    client = AsyncQdrantClient(location=":memory:")
    try:
        knowledge = KnowledgeRepository(state.tasks.session_factory)
        identifier = str(uuid4())
        document = await knowledge.create_document(
            document_id=identifier,
            project_id=project_id,
            original_filename="public-scope.txt",
            title="Public project scope",
            content_type="text/plain",
            storage_key=identifier,
            sha256=payload_digest(PUBLIC_KNOWLEDGE),
            size_bytes=len(PUBLIC_KNOWLEDGE.encode()),
        )
        async with state.tasks.session_factory() as session:
            row = await session.get(DocumentRow, identifier)
            assert row is not None
            row.status, row.chunk_count, row.embedding_model, row.index_version = (
                "ready",
                1,
                dense.model,
                2,
            )
            await session.commit()
        ready = replace(
            document, status="ready", chunk_count=1, embedding_model=dense.model, index_version=2
        )
        chunk = DocumentChunkRecord(
            id=str(uuid4()),
            document_id=identifier,
            project_id=project_id,
            ordinal=0,
            page=1,
            section=None,
            content=PUBLIC_KNOWLEDGE,
            token_count=32,
            created_at=datetime.now(UTC),
        )
        index = QdrantChunkIndex(
            client, collection="workflow-public", dense_dimensions=dense.dimensions
        )
        await index.replace_document(
            ready,
            [chunk],
            await dense.embed_documents([PUBLIC_KNOWLEDGE]),
            await sparse.embed_documents([PUBLIC_KNOWLEDGE]),
        )
        yield RAGQueryService(
            provider=NoGeneration(),
            embeddings=dense,
            sparse_embeddings=sparse,
            reranker=LexicalReranker(),
            index=index,
            knowledge=knowledge,
            tasks=state.tasks,
            default_top_k=5,
            candidate_limit=5,
        )
    finally:
        await client.close()
