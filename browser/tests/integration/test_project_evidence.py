"""Opt-in disposable PostgreSQL/Qdrant validation; no external model services.

Set EVIDENCE_TEST_POSTGRES_URL to a disposable admin DB, and optionally
EVIDENCE_TEST_QDRANT_URL. Each test creates/drops its own database/collection.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.evidence.database import EvidencePublicationRow, ProjectClaimRow, SourceVersionRow
from autoscholar.evidence.index import EvidenceIndex
from autoscholar.orchestration.durable import DurableService
from autoscholar.rag.embeddings import FastEmbedProvider
from tests.test_project_evidence_reuse import ReusingProvider, published
from tests.test_research_quality import finish

pytestmark = pytest.mark.skipif(
    not os.environ.get("EVIDENCE_TEST_POSTGRES_URL"), reason="Disposable test database not supplied"
)


@pytest.fixture
async def postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    admin_url = make_url(os.environ["EVIDENCE_TEST_POSTGRES_URL"])
    name = "evidence_test_" + uuid4().hex
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    case_url = admin_url.set(database=name)
    engine = create_async_engine(case_url)
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url", case_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    try:
        await asyncio.to_thread(command.upgrade, config, "head")
        # Round trip the new schema in a real PostgreSQL database.
        await asyncio.to_thread(command.downgrade, config, "20261107_0019")
        await asyncio.to_thread(command.upgrade, config, "head")
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def repository() -> tuple[AgentTaskRepository, AsyncEngine]:
            return AgentTaskRepository(sessions), engine

        monkeypatch.setattr("tests.test_autonomous.repository", repository)
        yield sessions
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_postgres_reuse_and_concurrent_publication(postgres: Any, tmp_path: Path) -> None:
    service, engine, library, first = await published(tmp_path)
    try:
        async with postgres() as session:
            queue = await session.get(EvidencePublicationRow, first)
            assert queue
            queue.status = "pending"
            await session.commit()
        assert any(await asyncio.gather(library.publish_one(), library.publish_one()))
        async with postgres() as session:
            assert await session.scalar(select(func.count()).select_from(ProjectClaimRow)) == 2
            assert await session.scalar(select(func.count()).select_from(SourceVersionRow)) == 2
        objective = "Compare LoRA adaptation methods"
        service.provider.provider = ReusingProvider(objective)
        second, _ = await DurableService(service).submit(
            dict(
                objective=objective,
                workflow_kind="comparison",
                project_id="research-project",
                research_depth="standard",
                reuse_project_evidence=True,
            ),
            "postgres-second",
        )
        snapshot, job = await finish(service, second)
        assert job.status == "succeeded", job.error_code
        assert snapshot.evidence_context.claims and job.usage.get("search_queries", 0) == 0
        await library.publish_one()
        await WorkbenchRepository(postgres).delete_task("research-project", first)
        async with postgres() as session:
            rows = list(await session.scalars(select(ProjectClaimRow)))
            assert rows and not any(
                [
                    await library.valid_claim(session, row, project_id="research-project")
                    for row in rows
                ]
            )
    finally:
        await engine.dispose()


async def test_postgres_publication_deletion_race_never_leaves_reusable_claims(
    postgres: Any, tmp_path: Path
) -> None:
    _, engine, library, first = await published(tmp_path)
    try:
        async with postgres() as session:
            queue = await session.get(EvidencePublicationRow, first)
            assert queue
            queue.status = "pending"
            await session.commit()
        await asyncio.wait_for(
            asyncio.gather(
                library.publish_one(),
                WorkbenchRepository(postgres).delete_task("research-project", first),
            ),
            15,
        )
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


class LocalVectors(FastEmbedProvider):
    """Deterministic vectors exercise Qdrant independently of model downloads."""

    def __init__(self) -> None:
        super().__init__(
            model="local-fixture", dimensions=3, cache_dir=Path("."), local_files_only=True
        )

    def cached_copy(self) -> FastEmbedProvider:
        return self

    async def require_cached_model(self) -> None:
        pass

    async def embed_documents(self, docs: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in docs]

    async def embed_query(self, query: str) -> list[float]:
        return [1.0, 0.0, 0.0]


async def test_qdrant_index_scopes_projects_and_removes_withdrawn_claims(
    postgres: Any, tmp_path: Path
) -> None:
    url = os.environ.get("EVIDENCE_TEST_QDRANT_URL")
    if not url:
        pytest.skip("Disposable Qdrant not supplied")
    _, engine, library, _ = await published(tmp_path)
    client = AsyncQdrantClient(url=url)
    index = EvidenceIndex(library, client, LocalVectors(), "evidence_test_" + uuid4().hex)
    try:
        assert await index.run_once()
        assert await index.search("research-project", "LoRA adaptation", limit=10)
        assert not await index.search("different-project", "LoRA adaptation", limit=10)
        library.semantic = index
        selected = await library.select(
            "research-project", "lowrank", document_ids=[], source_types=["web", "paper"]
        )
        assert selected.claims
        for claim in selected.claims:
            await library.set_enabled("research-project", claim.id, False)
        assert await index.run_once()
        assert not await index.search("research-project", "LoRA adaptation", limit=10)
        assert not (
            await library.select(
                "research-project",
                "LoRA adaptation",
                document_ids=[],
                source_types=["web", "paper"],
            )
        ).claims
    finally:
        if await client.collection_exists(index.collection):
            await client.delete_collection(index.collection)
        await client.close()
        await engine.dispose()
