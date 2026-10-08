"""Isolated stack test entrypoint; scripted models, real DB/Redis/Qdrant/Docker.

Never installed into the production API. Compose mounts only this public fixture
and source/migrations, not the repository root, .env, or design document.
"""

import asyncio
import hashlib
import io
import json
import re
import shutil
import signal
import sys
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI

from autoscholar.core.config import Settings
from autoscholar.rag.embeddings import SparseVectorData
from autoscholar.rag.models import RetrievedChunk


class PublicEmbeddings:
    """Deterministic test vectors, NOT semantic-quality acceptance."""

    model = "public-fixture-hash-8"
    dimensions = 8

    @staticmethod
    def vector(value: str) -> list[float]:
        return [(byte + 1) / 256 for byte in hashlib.sha256(value.encode()).digest()[:8]]

    async def embed_documents(self, documents: Sequence[str]) -> list[list[float]]:
        return [self.vector(value) for value in documents]

    async def embed_query(self, query: str) -> list[float]:
        return self.vector(query)

    async def close(self) -> None:
        pass


class PublicSparse:
    model = "public-fixture-token-count"

    async def embed_query(self, query: str) -> SparseVectorData:
        counts = Counter(
            int.from_bytes(hashlib.sha256(token.encode()).digest()[:2], "big")
            for token in re.findall(r"\w+", query.lower())
        )
        indices = sorted(counts)
        return SparseVectorData(indices, [float(counts[index]) for index in indices])

    async def embed_documents(self, documents: Sequence[str]) -> list[SparseVectorData]:
        return [await self.embed_query(value) for value in documents]

    async def close(self) -> None:
        pass


class PublicReranker:
    model = "public-fixture-no-rerank"

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, limit: int
    ) -> list[RetrievedChunk]:
        return list(chunks[:limit])

    async def close(self) -> None:
        pass


def settings() -> Settings:
    result = Settings(_env_file=None)  # type: ignore[call-arg]
    assert result.app_env == "test"
    assert (
        result.database_url
        == "postgresql+asyncpg://acceptance:public-test-only@postgres:5432/acceptance"
    )
    assert (
        result.llm_api_key and result.llm_api_key.get_secret_value() == "public-offline-placeholder"
    )
    assert (
        result.experiment_api_token
        and result.experiment_api_token.get_secret_value() == "public-phase9g-token"
    )
    assert not any(
        (
            result.tavily_api_key,
            result.semantic_scholar_api_key,
            result.embedding_api_key,
            result.qdrant_api_key,
        )
    )
    assert result.workspace_root == Path("/validation/workspaces")
    return result


def create_application() -> FastAPI:
    from unittest.mock import patch

    config = settings()
    # main creates a module-level default app on import: no provider config is
    # ever read from a .env. Actual fixture services receive explicit injection.
    with patch("autoscholar.core.config.get_settings", return_value=config):
        from autoscholar.main import create_app
        from autoscholar.orchestration.smoke import OfflineCoordinator
    return create_app(
        config,
        llm_provider=OfflineCoordinator(),
        research_services=[],
        embedding_provider=PublicEmbeddings(),
        sparse_embedding_provider=PublicSparse(),
        reranker=PublicReranker(),
    )


def dataset_copy() -> None:
    source, target = Path("/public"), Path("/datasets/mnist")
    manifest = json.loads((source / ".autoscholar-ready").read_text(encoding="utf-8"))
    assert manifest["dataset_id"] == "mnist"
    files = []
    for name in (
        "train-images-idx3-ubyte",
        "train-labels-idx1-ubyte",
        "t10k-images-idx3-ubyte",
        "t10k-labels-idx1-ubyte",
    ):
        for suffix in ("", ".gz"):
            relative = Path("MNIST/raw") / (name + suffix)
            original = source / relative
            if original.is_file():
                assert not original.is_symlink()
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, destination)
                files.append(relative)
        assert (target / "MNIST/raw" / name).is_file()
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.as_posix().encode())
        with (target / relative).open("rb") as handle:
            for block in iter(lambda: handle.read(1048576), b""):
                digest.update(block)
    assert digest.hexdigest() == manifest["dataset_sha256"], "Public dataset digest mismatch"
    (target / ".autoscholar-ready").write_text(json.dumps(manifest), encoding="utf-8")
    print("public_dataset_copy: verified; source_read_only", flush=True)


def public_pdf() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

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
    content = DecodedStreamObject()
    text = b"Public MNIST scientific text for browser acceptance with searchable evidence. " * 6
    content.set_data(b"BT /F1 12 Tf 20 100 Td (" + text + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def serve(mode: str) -> None:
    app = create_application()
    if mode == "api":
        import uvicorn
        from fastapi.responses import Response

        @app.get("/__acceptance/public-pdf")
        async def pdf() -> Response:
            return Response(public_pdf(), media_type="application/pdf")

        await uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning", access_log=False)
        ).serve()
        return
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    async with app.router.lifespan_context(app):
        if mode == "documents":
            from autoscholar.rag.chunking import StructureAwareChunker
            from autoscholar.rag.parser import PDFParser
            from autoscholar.rag.worker import DocumentWorker

            worker = DocumentWorker(
                repository=app.state.knowledge_repository,
                storage=app.state.document_storage,
                parser=PDFParser(),
                chunker=StructureAwareChunker(max_tokens=32, overlap_tokens=4),
                embeddings=app.state.embedding_provider,
                index=app.state.chunk_index,
                sparse_embeddings=app.state.sparse_embedding_provider,
                lease_seconds=30,
                max_attempts=1,
                parse_timeout_seconds=5,
                max_pages=10,
            )
            await app.state.chunk_index.ensure_collection()
            run_once = worker.run_once
        else:
            assert mode == "workflow"
            run_once = app.state.durable_service.tick
        while not stop.is_set():
            tick = asyncio.create_task(run_once())
            stopping = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({tick, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                tick.cancel()
            stopping.cancel()
            results = await asyncio.gather(tick, stopping, return_exceptions=True)
            if isinstance(results[0], Exception):
                raise results[0]
            if not stop.is_set() and results[0] is not True:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.2)


async def ingress() -> None:
    """Only this test proxy can publish a port; API/workers retain no egress."""
    import anyio
    import httpx
    import uvicorn
    from fastapi import Request
    from starlette.responses import Response, StreamingResponse

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(75, connect=5)) as client:

        @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT"])
        async def forward(path: str, request: Request) -> Response:
            if not (path.startswith("workbench/") or path == "__acceptance/public-pdf"):
                return Response(status_code=404)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 53 * 1024 * 1024:
                    return Response(status_code=413)
            url = "http://api:8000" + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            headers = {
                key: value
                for key, value in request.headers.items()
                if key
                in {"authorization", "accept", "content-type", "idempotency-key", "last-event-id"}
            }
            try:
                response = await client.send(
                    client.build_request(
                        request.method, url, headers=headers, content=bytes(chunks)
                    ),
                    stream=True,
                )
            except httpx.HTTPError:
                return Response(
                    '{"error":{"code":"fixture_upstream_unavailable"}}',
                    status_code=503,
                    media_type="application/json",
                )

            async def body() -> AsyncIterator[bytes]:
                try:
                    async for chunk in response.aiter_raw():
                        yield chunk
                finally:
                    with anyio.CancelScope(shield=True):
                        await response.aclose()

            public_headers = {
                key: value
                for key, value in response.headers.items()
                if key not in {"connection", "transfer-encoding", "keep-alive"}
            }
            return StreamingResponse(
                body(), status_code=response.status_code, headers=public_headers
            )

        await uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning", access_log=False)
        ).serve()


async def database_checks() -> None:
    from sqlalchemy import text

    from autoscholar.agent.repository import AgentTaskRepository
    from autoscholar.infrastructure import Database
    from autoscholar.orchestration.durable_repository import DurableRepository

    db = Database(settings().database_url)
    try:
        tasks = AgentTaskRepository(db.session_factory)
        durable = DurableRepository(db.session_factory)
        await tasks.create_task(task_id="pg-concurrency", objective="public", mode="autonomous")

        async def writer() -> None:
            for _ in range(3):
                async with db.session_factory() as session:
                    await durable.event(session, "pg-concurrency", "budget_saved")
                    await session.commit()

        await asyncio.gather(*(writer() for _ in range(8)))
        async with db.session_factory() as session:
            await durable.event(session, "pg-concurrency", "budget_saved")
            await session.rollback()
        async with db.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT sequence FROM workflow_events "
                            "WHERE task_id='pg-concurrency' ORDER BY sequence"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert rows == list(range(1, 25)), rows
            assert (
                await session.scalar(
                    text("SELECT event_sequence FROM agent_tasks WHERE id='pg-concurrency'")
                )
                == 24
            )
        print("postgres_concurrent_events: 24 unique contiguous; rollback: passed", flush=True)
    finally:
        await db.close()
    # Real PostgreSQL row-lock/idempotency validation with the production router.
    # Workers have not started, so this cannot execute a provider or training.
    import httpx

    app = create_application()
    headers = {
        "Authorization": "Bearer public-phase9g-token",
        "Idempotency-Key": "public-pg-duplicate",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated"
        ) as client,
    ):
        submitted = await asyncio.gather(
            *(
                client.post(
                    "/workbench/agent/tasks",
                    headers=headers,
                    json={"objective": "public guarded cancellation", "mode": "autonomous"},
                )
                for _ in range(2)
            )
        )
        assert all(response.status_code == 202 for response in submitted)
        receipts = [response.json() for response in submitted]
        assert receipts[0]["task_id"] == receipts[1]["task_id"]
        assert sorted(receipt["created"] for receipt in receipts) == [False, True]
        identifier = receipts[0]["task_id"]
        state = (
            await client.get(f"/workbench/tasks/{identifier}/controls", headers=headers)
        ).json()
        cancelled = await asyncio.gather(
            *(
                client.post(
                    f"/workbench/tasks/{identifier}/control/cancel",
                    headers=headers,
                    json={"expected": state["expected"]},
                )
                for _ in range(2)
            )
        )
        assert sorted(response.status_code for response in cancelled) == [200, 409]
        assert (
            next(response for response in cancelled if response.status_code == 409).json()["error"][
                "code"
            ]
            == "workbench_state_stale"
        )
        # Same job status/checkpoint, but a separately committed event also
        # invalidates an earlier control receipt.
        stale_headers = {**headers, "Idempotency-Key": "public-pg-event-stale"}
        created = await client.post(
            "/workbench/agent/tasks",
            headers=stale_headers,
            json={"objective": "public event-only stale state", "mode": "autonomous"},
        )
        identifier = created.json()["task_id"]
        state = (
            await client.get(f"/workbench/tasks/{identifier}/controls", headers=headers)
        ).json()
        async with app.state.agent_repository.session_factory() as session:
            await app.state.durable_service.repository.event(session, identifier, "budget_saved")
            await session.commit()
        result = await client.post(
            f"/workbench/tasks/{identifier}/control/pause",
            headers=headers,
            json={"expected": state["expected"]},
        )
        assert (
            result.status_code == 409 and result.json()["error"]["code"] == "workbench_state_stale"
        )
        state = (
            await client.get(f"/workbench/tasks/{identifier}/controls", headers=headers)
        ).json()
        assert (
            await client.post(
                f"/workbench/tasks/{identifier}/control/cancel",
                headers=headers,
                json={"expected": state["expected"]},
            )
        ).status_code == 200
    print(
        "postgres_duplicate_submission: one root; concurrent_controls: one accepted; "
        "event_only_stale: rejected",
        flush=True,
    )


async def backfill_check() -> None:
    from sqlalchemy import text

    from autoscholar.infrastructure import Database

    db = Database(settings().database_url)
    try:
        async with db.session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261006_0013"
            )
            values = (
                (
                    await session.execute(
                        text(
                            "SELECT sequence FROM workflow_events "
                            "WHERE task_id='pg-concurrency' ORDER BY sequence"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert values == list(range(1, 25))
            assert (
                await session.scalar(
                    text("SELECT event_sequence FROM agent_tasks WHERE id='pg-concurrency'")
                )
                == 24
            )
        print("postgres_migration_roundtrip_backfill: passed", flush=True)
    finally:
        await db.close()


async def persistence_check() -> None:
    import httpx
    from sqlalchemy import select

    from autoscholar.agent.database_models import AgentTaskRow
    from autoscholar.rag.database_models import DocumentRow

    app = create_application()
    headers = {"Authorization": "Bearer public-phase9g-token"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(base_url="http://api:8000", timeout=15) as client,
    ):
        async with app.state.agent_repository.session_factory() as session:
            task = await session.scalar(
                select(AgentTaskRow).where(
                    AgentTaskRow.parent_task_id.is_(None),
                    AgentTaskRow.status == "succeeded",
                    AgentTaskRow.objective.like("Public MNIST%"),
                )
            )
            assert task is not None and task.project_id
            document = await session.scalar(
                select(DocumentRow).where(
                    DocumentRow.project_id == task.project_id, DocumentRow.status == "ready"
                )
            )
            assert document is not None and document.chunk_count > 0
        for _ in range(3):
            overview = (
                await client.get(f"/workbench/tasks/{task.id}/overview", headers=headers)
            ).json()
            assert overview["execution"]["status"] == "succeeded"
            assert overview["execution"]["budget_used"]["training_runs"] == 1
            await asyncio.sleep(0.2)
        manifest = (
            await client.get(f"/workbench/tasks/{task.id}/resources/artifacts", headers=headers)
        ).json()["items"]
        report = next(item for item in manifest if item["type"] == "report")
        response = await client.get(
            f"/workbench/tasks/{task.id}/resources/artifacts/{report['id']}/content",
            headers=headers,
        )
        assert response.status_code == 200
        assert (
            len(response.content) == report["size_bytes"]
            and hashlib.sha256(response.content).hexdigest() == report["sha256"]
        )
        events = (await client.get(f"/workbench/tasks/{task.id}/events", headers=headers)).json()
        cursor = events["next_cursor"]
        async with client.stream(
            "GET",
            f"/workbench/tasks/{task.id}/stream",
            headers={**headers, "Last-Event-ID": str(cursor - 1)},
        ) as stream:
            assert stream.status_code == 200
            data = (await stream.aread()).decode()
            assert f"id: {cursor}\n" in data and '"reason":"terminal"' in data
        hits = await app.state.chunk_index.search_hybrid(
            project_id=task.project_id,
            document_ids=[document.id],
            dense_vector=await app.state.embedding_provider.embed_query("Public MNIST"),
            sparse_vector=await app.state.sparse_embedding_provider.embed_query("Public MNIST"),
            limit=5,
        )
        assert hits and all(
            hit.project_id == task.project_id and hit.document_id == document.id for hit in hits
        )
        assert await app.state.redis.ping()
        await app.state.redis.set_json("public-phase9-cache", {"verified": True}, ttl_seconds=30)
        assert await app.state.redis.get_json("public-phase9-cache") == {"verified": True}
    print(
        "api_worker_restart: state/report retained; training_runs:1; SSE replay: passed; "
        "Qdrant filtered hybrid: passed; Redis read/write: passed",
        flush=True,
    )


async def main() -> None:
    mode = sys.argv[1]
    if mode == "dataset-copy":
        dataset_copy()
    elif mode == "database-checks":
        await database_checks()
    elif mode == "backfill-check":
        await backfill_check()
    elif mode == "persistence-check":
        await persistence_check()
    elif mode == "ingress":
        await ingress()
    else:
        await serve(mode)


if __name__ == "__main__":
    asyncio.run(main())
