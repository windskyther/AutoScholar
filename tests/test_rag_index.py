from datetime import UTC, datetime

from qdrant_client import AsyncQdrantClient, models

from autoscholar.rag.index import QdrantChunkIndex
from autoscholar.rag.models import DocumentChunkRecord, DocumentRecord


async def test_qdrant_index_replaces_and_filters_document_points() -> None:
    client = AsyncQdrantClient(":memory:")
    index = QdrantChunkIndex(client, collection="test_chunks", dense_dimensions=2)
    now = datetime.now(UTC)
    document = DocumentRecord(
        id="00000000-0000-0000-0000-000000000001",
        project_id="project-1",
        original_filename="paper.pdf",
        title="LoRA",
        content_type="application/pdf",
        storage_key="paper.pdf",
        sha256="a" * 64,
        size_bytes=10,
        status="processing",
        page_count=None,
        chunk_count=0,
        embedding_model=None,
        index_version=0,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )
    chunk = DocumentChunkRecord(
        id="00000000-0000-0000-0000-000000000002",
        document_id=document.id,
        project_id=document.project_id,
        ordinal=0,
        page=3,
        section="Method",
        content="Low-rank adapters freeze the base model.",
        token_count=7,
        created_at=now,
    )

    await index.replace_document(document, [chunk], [[1.0, 0.0]])
    response = await client.query_points(
        "test_chunks",
        query=[1.0, 0.0],
        using="dense",
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="project_id", match=models.MatchValue(value="project-1")
                )
            ]
        ),
        limit=5,
        with_payload=True,
    )

    assert len(response.points) == 1
    assert response.points[0].payload is not None
    assert response.points[0].payload["document_id"] == document.id
    assert response.points[0].payload["page"] == 3

    await index.delete_document(document.project_id, document.id)
    empty = await client.query_points(
        "test_chunks", query=[1.0, 0.0], using="dense", limit=5
    )
    assert empty.points == []
    await client.close()
