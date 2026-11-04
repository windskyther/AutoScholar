"""Isolated production-settings ingress fixture: real stores, no paid providers."""

import asyncio
import hashlib
import io
import signal
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import Response
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from autoscholar.core.config import Settings
from autoscholar.deployment.worker_health import heartbeat
from autoscholar.llm import LLMResult
from autoscholar.orchestration.checkpoints import digest
from autoscholar.orchestration.durable_models import WorkflowApprovalRow
from autoscholar.orchestration.models import PlanStep, TaskPlan
from autoscholar.rag.embeddings import SparseVectorData
from autoscholar.rag.models import RetrievedChunk


class PublicEmbeddings:
    model = "public-ingress-fixture-8"
    dimensions = 8

    @staticmethod
    def vector(value: str) -> list[float]:
        return [(byte + 1) / 256 for byte in hashlib.sha256(value.encode()).digest()[:8]]

    async def embed_documents(self, values: Sequence[str]) -> list[list[float]]:
        return [self.vector(value) for value in values]

    async def embed_query(self, value: str) -> list[float]:
        return self.vector(value)

    async def close(self) -> None:
        pass


class PublicSparse:
    model = "public-sparse"

    async def embed_query(self, value: str) -> SparseVectorData:
        return SparseVectorData([1], [1.0])

    async def embed_documents(self, values: Sequence[str]) -> list[SparseVectorData]:
        return [await self.embed_query(value) for value in values]

    async def close(self) -> None:
        pass


class PublicReranker:
    model = "public-reranker"

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        return list(chunks[:limit])

    async def close(self) -> None:
        pass


class NoProvider:
    # Advertise a fixture capability to test submission UI; dispatch always raises.
    # The dedicated workflow worker never claims jobs, and the stack cannot egress.
    configured = True
    calls = 0

    async def generate(self, *args: Any, **kwargs: Any) -> LLMResult:
        self.calls += 1
        raise RuntimeError("Ingress fixture forbids model dispatch")

    async def close(self) -> None:
        pass


class IngressSettings(Settings):
    @property
    def llm_configured(self) -> bool:
        # Enable the submission UI for the injected local provider, with no API key.
        return True


def public_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 20 100 Td (Public MNIST production ingress PDF fixture. "
        b"Scientific validation text. Compare two public MNIST classifiers using fixed "
        b"train and test splits, deterministic random seeds, accuracy, duration and "
        b"resource measurements. This public document is generated only for isolated "
        b"ingress acceptance and contains no model credentials or private research.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def create_app() -> FastAPI:
    config = Settings(_env_file=None)  # type: ignore[call-arg]
    assert config.app_env == "production" and config.embedding_dimensions == 8
    assert not any(
        (
            config.llm_api_key,
            config.tavily_api_key,
            config.semantic_scholar_api_key,
            config.embedding_api_key,
        )
    )
    with patch("autoscholar.core.config.get_settings", return_value=config):
        from autoscholar.main import create_app as factory
    config = IngressSettings(_env_file=None)  # type: ignore[call-arg]
    provider = NoProvider()
    app = factory(
        config,
        llm_provider=provider,
        research_services=[],
        embedding_provider=PublicEmbeddings(),
        sparse_embedding_provider=PublicSparse(),
        reranker=PublicReranker(),
    )
    original = app.router.lifespan_context
    state: dict[str, str] = {}

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> Any:
        async with original(application):
            project = await app.state.knowledge_repository.create_project(
                name="中文生产验收项目", description="临时隔离栈"
            )
            state["project_id"] = project.id
            durable = app.state.durable_service
            task, _ = await durable.submit(
                {"mode": "autonomous", "objective": "公开生产入口验收", "project_id": project.id},
                f"production-seed-{project.id}",
            )
            state["task_id"] = task
            snapshot, _ = await durable.repository.snapshot(task)
            snapshot.plan = TaskPlan(
                goal="公开生产入口验收",
                steps=[
                    PlanStep(
                        id="code", type="coding", description="public", expected_output="code"
                    ),
                    PlanStep(
                        id="train",
                        type="experiment",
                        dependencies=["code"],
                        description="public",
                        expected_output="metrics",
                    ),
                ],
            )
            snapshot.stage = "executor"
            await durable.service.workflows.save_plan(task, 1, snapshot.plan, "public fixture")
            operation = {
                "task_id": task,
                "plan_version": 1,
                "step_id": "train",
                "operation": "experiment",
                "risk_level": 3,
                "specification": snapshot.specification.model_dump(),
                "budget_limits": snapshot.limits.model_dump(),
                "source_sha256": "0" * 64,
                "cost_units": snapshot.specification.epochs
                * snapshot.specification.train_samples
                * 2,
            }
            async with durable.repository.sessions() as session:
                job = await durable.repository.locked(session, task)
                durable.repository.checkpoint(session, job, snapshot)
                await durable.repository.status(session, job, "awaiting_approval")
                session.add(
                    WorkflowApprovalRow(
                        id=str(uuid4()),
                        task_id=task,
                        operation_sha256=digest(operation),
                        payload=operation,
                        status="pending",
                        reason="公开夹具，不会执行训练",  # noqa: RUF001
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
                await durable.repository.event(session, task, "approval_requested")
                await session.commit()
            child = str(uuid4())
            from autoscholar.core.budget import current_parent

            token = current_parent.set(task)
            try:
                await app.state.agent_repository.create_task(
                    task_id=child,
                    objective="public artifact",
                    mode="experiment",
                    project_id=project.id,
                )
            finally:
                current_parent.reset(token)
            app.state.workspace_manager.initialize(child)
            experiment = await app.state.agent_repository.create_experiment(
                task_id=child, name="public", specification=snapshot.specification.model_dump()
            )
            artifact = await app.state.artifact_manager.save(
                child, experiment.id, "reports/report.md", "# 中文报告\n公开生产入口夹具。".encode()
            )
            state["artifact_id"] = artifact.id
            yield
            assert provider.calls == 0

    app.router.lifespan_context = lifespan

    @app.get("/__fixture/status")
    async def status() -> dict[str, Any]:
        return {**state, "supplier_api_calls": provider.calls}

    @app.get("/__fixture/pdf")
    async def pdf() -> Response:
        return Response(public_pdf(), media_type="application/pdf")

    @app.post("/__fixture/event")
    async def event() -> dict[str, int]:
        durable = app.state.durable_service
        async with durable.repository.sessions() as session:
            await durable.repository.event(
                session,
                state["task_id"],
                "checkpoint_saved",
                stage="executor",
                status="awaiting_approval",
            )
            await session.commit()
        return {"ok": 1}

    return app


async def documents() -> None:
    from autoscholar.infrastructure import Database, Qdrant
    from autoscholar.rag import KnowledgeRepository, LocalDocumentStorage, QdrantChunkIndex
    from autoscholar.rag.chunking import StructureAwareChunker
    from autoscholar.rag.parser import PDFParser
    from autoscholar.rag.worker import DocumentWorker

    config = Settings(_env_file=None)  # type: ignore[call-arg]
    database = Database(config.database_url)
    assert config.qdrant_api_key
    qdrant = Qdrant(config.qdrant_url, api_key=config.qdrant_api_key.get_secret_value())
    worker = DocumentWorker(
        repository=KnowledgeRepository(database.session_factory),
        storage=LocalDocumentStorage(config.document_storage_path),
        parser=PDFParser(),
        chunker=StructureAwareChunker(max_tokens=96, overlap_tokens=16),
        embeddings=PublicEmbeddings(),
        index=QdrantChunkIndex(
            qdrant.client, collection=config.qdrant_collection, dense_dimensions=8
        ),
        sparse_embeddings=PublicSparse(),
        lease_seconds=30,
        max_attempts=1,
        parse_timeout_seconds=10,
        max_pages=10,
    )
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    try:
        async with heartbeat():
            while not stop.is_set():
                await worker.run_once()
                await asyncio.sleep(0.2)
    finally:
        await qdrant.close()
        await database.close()


async def hold_workflow() -> None:
    # Ingress acceptance exercises control and durable receipts, not autonomous execution.
    # Keep model dispatch impossible even when a browser submits an authorized task.
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    async with heartbeat():
        await stop.wait()


if __name__ == "__main__":
    import sys

    asyncio.run(documents() if sys.argv[1] == "documents" else hold_workflow())
