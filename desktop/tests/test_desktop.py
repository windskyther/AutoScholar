import asyncio
import threading
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from autoscholar.desktop.retrieval import Retrieval
from autoscholar.desktop.runtime import desktop_app
from autoscholar.desktop.storage import DesktopDatabase
from autoscholar.desktop.transfer import export, import_library
from autoscholar.rag.database_models import ProjectRow

TOKEN = "2" * 64


async def test_cache_clear_waits_for_idle_and_excludes_new_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = Retrieval(tmp_path, tmp_path / "unused.exe")
    cache = tmp_path / "models"
    cache.mkdir()
    weights = cache / "weights"
    weights.write_bytes(b"fixture")
    retrieval.pending = 1
    assert not await retrieval.clear_model_cache()
    assert weights.exists()
    retrieval.pending = 0
    started = threading.Event()
    finish = threading.Event()

    def close_idle_worker() -> None:
        started.set()
        assert finish.wait(5)

    monkeypatch.setattr(retrieval, "close", close_idle_worker)
    clear = asyncio.create_task(retrieval.clear_model_cache())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert not await retrieval.clear_model_cache()
        queued = asyncio.create_task(retrieval.call("document"))
        await asyncio.sleep(0)
        assert not queued.done() and retrieval.pool is None
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    finally:
        finish.set()
        assert await clear
    assert not cache.exists() and retrieval.cache_available.is_set()


async def test_desktop_lock_serializes_competing_writers(tmp_path: Path) -> None:
    database = DesktopDatabase(tmp_path / "db.sqlite3")
    await database.initialize()
    started = asyncio.Event()

    async def rename_again() -> str:
        async with database.session_factory.begin() as session:
            started.set()
            row = (await session.scalars(select(ProjectRow).with_for_update())).one()
            previous = row.name
            row.name += " again"
            return previous

    try:
        async with database.session_factory.begin() as session:
            session.add(ProjectRow(id="one", name="original"))
        async with database.session_factory.begin() as session:
            row = (await session.scalars(select(ProjectRow).with_for_update())).one()
            other_writer = asyncio.create_task(rename_again())
            await started.wait()
            await asyncio.sleep(0.05)
            assert not other_writer.done()
            row.name = "changed"
        assert await asyncio.wait_for(other_writer, timeout=3) == "changed"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_real_desktop_session_persists_and_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "web"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("desktop", encoding="utf8")
    root = tmp_path / "source"
    root.mkdir()
    app = desktop_app(root, assets, root / "unused.exe", SecretStr("offline-test-key"), TOKEN, 8910)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8910"
        ) as client,
    ):
        assert (await client.get("/api/workbench/session")).status_code == 403
        models = root / "models"
        for directory in ("documents", "vectors"):
            sibling = root / directory
            sibling.mkdir(exist_ok=True)
            (sibling / "fixture").write_bytes(b"keep")
        cache_url = "/api/desktop/model-cache"
        assert (await client.delete(cache_url)).status_code == 403
        client.headers["X-AutoScholar-Session"] = TOKEN
        assert (await client.get("/api/workbench/session")).status_code == 200
        assert (await client.post("/api/workbench/agent/run")).status_code == 404
        assert (
            await client.get("/api/workbench/session", headers={"Origin": "http://other.local"})
        ).status_code == 403
        created = await client.post("/api/workbench/projects", json={"name": "Local research"})
        assert created.status_code == 201, created.text
        project = created.json()["id"]
        assert (await client.get(f"/api/workbench/projects/{project}")).json()[
            "name"
        ] == "Local research"

        from tests.test_agent_runner import ScriptedProvider
        from tests.test_research_agent import search_services
        from tests.test_research_workflow import OBJECTIVE, research_script

        provider = ScriptedProvider(research_script())
        monkeypatch.setattr("autoscholar.llm.profiles.create_llm_provider", lambda _: provider)
        runner = app.state.backend.state.agent_runner
        runner._research_services = {s.source_type: s for s in search_services()}
        profile = await client.post(
            "/api/workbench/models",
            json={
                "name": "Offline fixture",
                "base_url": "https://fixture.invalid/v1",
                "model": "fixture",
                "api_key": "local-test-key",
                "pricing": {"currency": "USD", "input_per_million": 2, "output_per_million": 3},
            },
        )
        assert profile.status_code == 201, profile.text
        submitted = await client.post(
            "/api/workbench/agent/tasks",
            json={
                "project_id": project,
                "objective": OBJECTIVE,
                "workflow_kind": "research",
                "mode": "autonomous",
                "model_profile_id": profile.json()["id"],
                "research_depth": "quick",
                "document_ids": [],
            },
            headers={"Idempotency-Key": "desktop-fixture"},
        )
        assert submitted.status_code == 202, submitted.text
        task_id = submitted.json()["task_id"]
        async with asyncio.timeout(45):
            while True:
                _, job = await app.state.backend.state.durable_service.repository.snapshot(task_id)
                if job.status not in ("queued", "running"):
                    break
                await asyncio.sleep(0.15)
        assert job.status == "succeeded", (job.status, job.error_code)
        models.mkdir()
        (models / "weights").write_bytes(b"model")
        assert (await client.get(cache_url)).json() == {"bytes": 5}
        for _ in range(2):
            assert (await client.delete(cache_url)).json() == {"bytes": 0}
        assert (await client.get(cache_url)).json() == {"bytes": 0}
        assert not models.exists()
        for directory in ("documents", "vectors"):
            assert (root / directory / "fixture").read_bytes() == b"keep"
        report = await client.get(f"/api/workbench/tasks/{task_id}/output/markdown")
        assert report.status_code == 200 and "LoRA" in report.text

    source = DesktopDatabase(root / "library.sqlite3")
    package = tmp_path / "research.zip"
    try:
        async with source.session_factory() as session:
            usage_query = text(
                "SELECT id, estimated_cost, total_tokens FROM model_usage ORDER BY id"
            )
            usage_before = (await session.execute(usage_query)).all()
            assert any(row.estimated_cost is not None for row in usage_before)
            await export(
                await session.connection(), package, root / "documents", root / "workspaces"
            )
    finally:
        await source.close()
    destination = await import_library(package, tmp_path)
    migrated = DesktopDatabase(destination / "library.sqlite3")
    try:
        async with migrated.session_factory() as session:
            assert await session.scalar(text("SELECT id FROM projects")) == project
            assert (await session.execute(usage_query)).all() == usage_before
            assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
    finally:
        await migrated.close()
