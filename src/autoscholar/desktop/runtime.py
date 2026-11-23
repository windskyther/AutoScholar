"""Same-origin desktop facade, backed by the shared research workbench."""

import asyncio
import hmac
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy import or_, select
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from autoscholar.api.governance import RequestGovernance
from autoscholar.core.audit import DatabaseAuditSink
from autoscholar.core.config import Settings
from autoscholar.desktop.local import Cache, DisabledSandbox, DisabledService
from autoscholar.desktop.retrieval import DesktopQuery, Retrieval
from autoscholar.desktop.storage import DesktopDatabase, LocalAdmission
from autoscholar.evidence.database import EvidencePublicationRow, ProjectClaimRow
from autoscholar.main import create_app
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.rag.database_models import DocumentJobRow

logger = logging.getLogger(__name__)


class DesktopSettings(Settings):
    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def desktop_settings(folder: Path, key: SecretStr) -> Settings:
    return DesktopSettings(
        model_encryption_key=key,
        api_allowed_hosts=["127.0.0.1"],
        document_storage_path=folder / "documents",
        workspace_root=folder / "workspaces",
        model_cache_path=folder / "models",
    )


class SessionBoundary:
    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app, self.token, self.host = app, token, f"127.0.0.1:{port}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            path = scope["path"]
            headers = request.headers
            if (
                headers.getlist("host") != [self.host]
                or headers.getlist("origin") not in ([], ["http://" + self.host])
                or headers.get("sec-fetch-site") == "cross-site"
            ):
                await JSONResponse({"error": {"code": "invalid_origin"}}, 403)(scope, receive, send)
                return
            if path.startswith("/api/"):
                if not hmac.compare_digest(
                    headers.get("x-autoscholar-session", "").encode(), self.token.encode()
                ):
                    await JSONResponse({"error": {"code": "local_access_denied"}}, 403)(
                        scope, receive, send
                    )
                    return
                if not re.fullmatch(
                    r"/api/(?:local-access|desktop/(?:settings|model-cache)|workbench/(?:session|usage|models(?:/.*)?|projects(?:/.*)?|tasks/.*|agent/tasks))",
                    path,
                ):
                    await JSONResponse({}, 404)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def desktop_app(
    folder: Path,
    assets: Path,
    executable: Path,
    key: SecretStr,
    token: str,
    port: int,
    *,
    idle: Callable[[], None] = lambda: None,
) -> FastAPI:
    from autoscholar.desktop.preferences import install_settings, search_keys

    settings = desktop_settings(folder, key)
    settings.tavily_api_key, settings.semantic_scholar_api_key = search_keys(folder)
    db = DesktopDatabase(folder / "library.sqlite3")
    retrieval = Retrieval(folder, executable)
    backend = create_app(
        settings,
        database=db,
        redis=DisabledService(),
        qdrant=DisabledService(),
        sandbox_executor=DisabledSandbox(),
        research_cache=Cache(db),
        governance=RequestGovernance(LocalAdmission(), DatabaseAuditSink(db.session_factory)),
        query_factory=lambda provider, tasks: DesktopQuery(provider, tasks, retrieval),
    )
    evidence = backend.state.evidence_library
    evidence.semantic = retrieval
    state = {"workflow": False, "documents": False}

    async def workflows() -> None:
        while True:
            try:
                state["workflow"] = True
                worked = await backend.state.durable_service.tick()
                published = await evidence.publish_one()
                if worked or published:
                    continue
            except Exception:
                logger.exception("Desktop workflow tick failed")
            finally:
                state["workflow"] = False
            await asyncio.sleep(1)

    async def documents() -> None:
        retry_evidence = 0.0
        while True:
            try:
                now = datetime.now(UTC)
                async with db.session_factory() as session:
                    job = await session.scalar(
                        select(DocumentJobRow.id)
                        .where(
                            or_(
                                (DocumentJobRow.status == "queued")
                                & (DocumentJobRow.available_at <= now),
                                (DocumentJobRow.status == "processing")
                                & (DocumentJobRow.lease_expires_at < now),
                            )
                        )
                        .limit(1)
                    )
                    pending = await session.scalar(
                        select(ProjectClaimRow.id)
                        .where(ProjectClaimRow.indexed_version == 0)
                        .limit(1)
                    )
                state["documents"] = True
                if job and await retrieval.call("document"):
                    continue
                if (
                    pending
                    and settings.model_cache_path.is_dir()
                    and time.monotonic() >= retry_evidence
                ):
                    if await retrieval.call("evidence"):
                        continue
                    retry_evidence = time.monotonic() + 300
            except Exception:
                logger.exception("Desktop document tick failed")
                await asyncio.sleep(15)
            finally:
                state["documents"] = False
            await asyncio.sleep(2)

    async def lifecycle() -> None:
        while True:
            await asyncio.sleep(2)
            retrieval.reap()
            async with db.session_factory() as session:
                active = await session.scalar(
                    select(WorkflowJobRow.task_id)
                    .where(
                        WorkflowJobRow.status.in_(
                            ["queued", "running", "pause_requested", "cancel_requested"]
                        )
                    )
                    .limit(1)
                )
                jobs = await session.scalar(
                    select(DocumentJobRow.id)
                    .where(DocumentJobRow.status.in_(["queued", "processing"]))
                    .limit(1)
                )
                publications = await session.scalar(
                    select(EvidencePublicationRow.task_id)
                    .where(
                        EvidencePublicationRow.status == "pending",
                        EvidencePublicationRow.attempts < 5,
                    )
                    .limit(1)
                )
            if not (
                active
                or jobs
                or publications
                or any(state.values())
                or retrieval.pending
                or not retrieval.cache_available.is_set()
                or getattr(app.state, "importing", False)
            ):
                idle()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await db.initialize()
            async with backend.router.lifespan_context(backend):
                tasks = [asyncio.create_task(fn()) for fn in (workflows, documents, lifecycle)]
                try:
                    yield
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await retrieval.cache_available.wait()
            retrieval.close()
            await db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.backend = backend
    app.add_middleware(SessionBoundary, token=token, port=port)

    @app.get("/api/local-access")
    async def access() -> dict[str, str]:
        return {"mode": "local_proxy"}

    @app.get("/api/desktop/model-cache")
    def model_cache() -> dict[str, int]:
        size = 0
        cache = settings.model_cache_path
        if cache.is_symlink() or cache.is_junction():
            raise HTTPException(409, "Model cache is a filesystem link")
        for folder_path, dirs, files in cache.walk():
            dirs[:] = [name for name in dirs if not (folder_path / name).is_junction()]
            for name in files:
                file = folder_path / name
                try:
                    if not file.is_symlink() and file.is_file():
                        size += file.stat().st_size
                except FileNotFoundError:
                    continue  # Downloads are renamed when they complete.
        return {"bytes": size}

    @app.delete("/api/desktop/model-cache")
    async def clear_model_cache() -> dict[str, int]:
        try:
            cleared = await retrieval.clear_model_cache()
        except OSError as exc:
            raise HTTPException(409, "model_cache_locked") from exc
        if not cleared:
            raise HTTPException(409, "model_cache_busy")
        return {"bytes": 0}

    install_settings(app, folder)
    app.mount("/api", backend)
    app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

    @app.get("/{path:path}")
    async def page(path: str) -> FileResponse:
        return FileResponse(
            assets / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
                "object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            },
        )

    return app
