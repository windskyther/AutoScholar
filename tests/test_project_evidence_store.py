"""Offline publication queue, source binding, deduplication and withdrawal."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, update

from autoscholar.agent.database_models import EvidenceRow
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.evidence.database import EvidencePublicationRow, ProjectClaimRow, SourceVersionRow
from autoscholar.evidence.models import content_hash
from autoscholar.evidence.store import EvidenceStore
from autoscholar.orchestration.durable import DurableService
from autoscholar.rag.database_models import ProjectRow
from tests.test_research_quality import QualityProvider, finish, setup
from tests.test_research_workflow import OBJECTIVE


async def completed(tmp_path: Path) -> tuple[Any, Any, EvidenceStore, str]:
    service, engine = await setup(tmp_path, QualityProvider())
    task_id, _ = await DurableService(service).submit(
        dict(
            objective=OBJECTIVE,
            workflow_kind="research",
            project_id="research-project",
            research_depth="standard",
        ),
        "bank-source",
    )
    snapshot, job = await finish(service, task_id)
    assert job.status == "succeeded", (job.error_code, snapshot)
    return service, engine, EvidenceStore(service.tasks.session_factory), task_id


async def test_publication_is_atomic_deduplicated_and_bound_to_report(tmp_path: Path) -> None:
    _service, engine, bank, task_id = await completed(tmp_path)
    try:
        async with bank.sessions() as session:
            publication = await session.get(EvidencePublicationRow, task_id)
            assert publication and publication.status == "pending"
            assert not await session.scalar(select(func.count()).select_from(ProjectClaimRow))
        assert await bank.publish_one()
        assert not await bank.publish_one()
        async with bank.sessions() as session:
            claims = list(await session.scalars(select(ProjectClaimRow)))
            assert len(claims) == 2
            assert await session.scalar(select(func.count()).select_from(SourceVersionRow)) == 2
            for claim in claims:
                assert await bank.valid_claim(session, claim, project_id="research-project")
                assert not await bank.valid_claim(session, claim, project_id="different-project")
                for link in claim.supports:
                    source = await session.get(SourceVersionRow, link["source_id"])
                    assert source and link["quote"] in source.material["text"]
            publication = await session.get(EvidencePublicationRow, task_id)
            assert publication
            publication.status = (
                "pending"  # Simulate an acknowledged publication being redelivered.
            )
            await session.commit()
        assert await bank.publish_one()
        async with bank.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ProjectClaimRow)) == 2
    finally:
        await engine.dispose()


@pytest.mark.parametrize("when", ["before", "after"])
async def test_deleted_task_cannot_publish_or_remain_reusable(tmp_path: Path, when: str) -> None:
    service, engine, bank, task_id = await completed(tmp_path)
    try:
        if when == "after":
            await bank.publish_one()
        repository = WorkbenchRepository(service.tasks.session_factory)
        await repository.delete_task("research-project", task_id)
        await bank.publish_one()
        async with bank.sessions() as session:
            for claim in await session.scalars(select(ProjectClaimRow)):
                assert not await bank.valid_claim(session, claim, project_id="research-project")
            if when == "before":
                assert not await session.scalar(select(func.count()).select_from(ProjectClaimRow))
        task = await service.tasks.get_task(task_id)
        assert task and task.status == "succeeded" and task.answer
    finally:
        await engine.dispose()


@pytest.mark.parametrize("tamper", ["excerpt", "checkpoint", "project"])
async def test_publication_refuses_changed_provenance_without_modifying_history(
    tmp_path: Path,
    tamper: str,
) -> None:
    service, engine, bank, task_id = await completed(tmp_path)
    try:
        task = await service.tasks.get_task(task_id)
        assert task
        before = task.answer
        async with bank.sessions() as session:
            if tamper == "excerpt":
                await session.execute(
                    update(EvidenceRow)
                    .where(EvidenceRow.task_id == task_id)
                    .values(excerpt="Forged content")
                )
            elif tamper == "checkpoint":
                publication = await session.get(EvidencePublicationRow, task_id)
                assert publication
                publication.audit_hash = "0" * 64
            else:
                project = await session.get(ProjectRow, "research-project")
                assert project
                from datetime import UTC, datetime

                project.deleted_at = datetime.now(UTC)
            await session.commit()
        assert await bank.publish_one()
        async with bank.sessions() as session:
            assert not await session.scalar(select(func.count()).select_from(ProjectClaimRow))
            publication = await session.get(EvidencePublicationRow, task_id)
            assert publication and publication.status in {"pending", "withdrawn"}
        after = await service.tasks.get_task(task_id)
        assert after and after.answer == before and after.status == "succeeded"
    finally:
        await engine.dispose()


async def test_source_content_changes_invalidate_claims(tmp_path: Path) -> None:
    _, engine, bank, _ = await completed(tmp_path)
    try:
        await bank.publish_one()
        async with bank.sessions() as session:
            claim = await session.scalar(select(ProjectClaimRow))
            assert claim
            source = await session.get(SourceVersionRow, claim.supports[0]["source_id"])
            assert source
            source.material = source.material | {"text": "Modified source"}
            assert content_hash(source.material) != source.content_hash
            await session.commit()
            assert not await bank.valid_claim(session, claim, project_id="research-project")
    finally:
        await engine.dispose()


def test_project_evidence_migration_preserves_existing_history(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + database.as_posix())
    with sqlite3.connect(database) as sql:
        sql.execute("CREATE TABLE model_profiles (id VARCHAR(36) PRIMARY KEY)")
        sql.execute("INSERT INTO model_profiles VALUES ('original')")
    command.stamp(config, "20261106_0016")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as sql:
        assert sql.execute("SELECT * FROM model_profiles").fetchall() == [("original",)]
        assert sql.execute("SELECT count(*) FROM project_claims").fetchone() == (0,)
    command.downgrade(config, "20261107_0019")
    with sqlite3.connect(database) as sql:
        assert sql.execute("SELECT * FROM model_profiles").fetchall() == [("original",)]
    command.upgrade(config, "head")
