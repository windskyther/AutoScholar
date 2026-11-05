from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.records import TaskStatus
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.budget import current_parent
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.rag.database_models import DocumentJobRow, DocumentRow, ProjectRow
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.tool_platform.operation_models import CoreToolCallRow
from tests.api_credentials import AUTH, TOKEN_SECRET
from tests.governance import offline_app
from tests.test_chat import SuccessfulProvider
from tests.test_health import FakeDependency


@pytest.fixture
async def stores() -> AsyncIterator[tuple[KnowledgeRepository, AgentTaskRepository]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield KnowledgeRepository(sessions), AgentTaskRepository(sessions)
    finally:
        await engine.dispose()


async def finish(tasks: AgentTaskRepository, task_id: str) -> None:
    await tasks.update_task(
        task_id, status="succeeded", plan=[], answer="Original result", metrics={}
    )


async def test_delete_hides_project_and_resources_without_erasing_task_history(
    stores: tuple[KnowledgeRepository, AgentTaskRepository],
) -> None:
    knowledge, tasks = stores
    project = await knowledge.create_project(name="Delete this", description=None)
    other = await knowledge.create_project(name="Keep this", description=None)
    await tasks.create_task(task_id="root", objective="Public fixture", project_id=project.id)
    await finish(tasks, "root")
    async with tasks.session_factory() as session:
        session.add(
            DocumentRow(
                id="doc",
                project_id=project.id,
                original_filename="public.pdf",
                title="Public",
                content_type="application/pdf",
                storage_key="doc.pdf",
                sha256="a" * 64,
                size_bytes=1,
                status="ready",
            )
        )
        await session.commit()
    await knowledge.delete_project(project.id)
    await knowledge.delete_project(project.id)
    assert await knowledge.get_project(project.id) is None
    assert await knowledge.list_projects(limit=20, offset=0) == ([other], 1)
    assert await knowledge.get_document(project.id, "doc") is None
    assert await knowledge.list_documents(project.id, limit=20, offset=0) == ([], 0)
    assert await knowledge.get_ready_documents(project.id) == []
    original = await tasks.get_task("root")
    assert original is not None and original.answer == "Original result"
    async with tasks.session_factory() as session:
        row = await session.get(ProjectRow, project.id)
        assert row is not None and row.deleted_at is not None
        assert await session.get(DocumentRow, "doc") is not None
    workbench = WorkbenchRepository(tasks.session_factory)
    for action in (workbench.overview("root", limit=20, offset=0), workbench.controls("root")):
        with pytest.raises(AppError, match="Project was not found"):
            await action
    with pytest.raises(AppError):
        await tasks.create_task(task_id="new", objective="Must not run", project_id=project.id)
    parent = current_parent.set("root")
    try:
        with pytest.raises(AppError):
            await tasks.create_task(task_id="new-child", objective="Must not run")
    finally:
        current_parent.reset(parent)
    with pytest.raises(AppError):
        await knowledge.enqueue_document_job("doc", kind="reindex")
    with pytest.raises(AppError):
        await knowledge.create_document(
            document_id="new-doc",
            project_id=project.id,
            original_filename="new.pdf",
            title="New",
            content_type="application/pdf",
            storage_key="new.pdf",
            sha256="b" * 64,
            size_bytes=1,
        )


@pytest.mark.parametrize(
    "status",
    ["running", "queued", "paused", "awaiting_approval", "recovery_required", "cancel_requested"],
)
async def test_active_tasks_prevent_project_deletion(
    stores: tuple[KnowledgeRepository, AgentTaskRepository],
    status: TaskStatus,
) -> None:
    knowledge, tasks = stores
    project = await knowledge.create_project(name="Still working", description=None)
    await tasks.create_task(task_id="root", objective="Public fixture", project_id=project.id)
    await tasks.update_task("root", status=status, plan=[], answer=None, metrics={})
    with pytest.raises(AppError) as caught:
        await knowledge.delete_project(project.id)
    assert caught.value.code == "project_has_active_tasks"
    assert await knowledge.get_project(project.id) is not None


async def test_running_child_without_project_prevents_deletion(
    stores: tuple[KnowledgeRepository, AgentTaskRepository],
) -> None:
    knowledge, tasks = stores
    project = await knowledge.create_project(name="Child still working", description=None)
    await tasks.create_task(task_id="root", objective="Root", project_id=project.id)
    parent = current_parent.set("root")
    try:
        await tasks.create_task(task_id="child", objective="Child")
    finally:
        current_parent.reset(parent)
    await finish(tasks, "root")
    with pytest.raises(AppError):
        await knowledge.delete_project(project.id)
    await finish(tasks, "child")
    await knowledge.delete_project(project.id)


@pytest.mark.parametrize("unknown_receipt", [False, True])
async def test_cancelled_workflow_with_unknown_calls_cannot_be_deleted(
    stores: tuple[KnowledgeRepository, AgentTaskRepository],
    unknown_receipt: bool,
) -> None:
    knowledge, tasks = stores
    project = await knowledge.create_project(name="Needs reconciliation", description=None)
    await tasks.create_task(task_id="root", objective="Public fixture", project_id=project.id)
    await finish(tasks, "root")
    async with tasks.session_factory() as session:
        session.add(
            WorkflowJobRow(
                task_id="root",
                idempotency_key="retained-key",
                request_sha256="a" * 64,
                status="cancelled",
                pending_calls={} if unknown_receipt else {"call": "unknown"},
            )
        )
        if unknown_receipt:
            session.add(
                CoreToolCallRow(
                    id="call",
                    task_id="root",
                    service="public-fixture",
                    tool="query",
                    arguments_sha256="a" * 64,
                    authority_sha256="b" * 64,
                    output_schema={},
                    status="unknown",
                )
            )
        await session.commit()
    with pytest.raises(AppError):
        await knowledge.delete_project(project.id)
    async with tasks.session_factory() as session:
        assert await session.get(WorkflowJobRow, "root") is not None


@pytest.mark.parametrize("status", ["queued", "processing"])
async def test_document_processing_prevents_deletion(
    stores: tuple[KnowledgeRepository, AgentTaskRepository],
    status: str,
) -> None:
    knowledge, tasks = stores
    project = await knowledge.create_project(name="Indexing", description=None)
    await knowledge.create_document(
        document_id="doc",
        project_id=project.id,
        original_filename="public.pdf",
        title="Public",
        content_type="application/pdf",
        storage_key="doc.pdf",
        sha256="a" * 64,
        size_bytes=1,
    )
    async with tasks.session_factory() as session:
        from sqlalchemy import update

        await session.execute(update(DocumentJobRow).values(status=status))
        await session.commit()
    with pytest.raises(AppError) as caught:
        await knowledge.delete_project(project.id)
    assert caught.value.code == "project_has_active_documents"


async def test_authenticated_delete_is_repeatable_and_deleted_project_is_not_browsable(
    stores: tuple[KnowledgeRepository, AgentTaskRepository],
    tmp_path: Path,
) -> None:
    knowledge, tasks = stores
    project = await knowledge.create_project(name="Public fixture", description=None)
    app = offline_app(
        Settings(
            experiment_api_token=TOKEN_SECRET,
            workspace_root=tmp_path / "workspaces",
            document_storage_path=tmp_path / "documents",
        ),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=SuccessfulProvider(),
        knowledge_repository=knowledge,
        agent_repository=tasks,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/workbench/projects/{project.id}"
        assert (await client.delete(path)).status_code == 401
        assert await knowledge.get_project(project.id) is not None
        assert (await client.delete(path, headers=AUTH)).status_code == 204
        assert (await client.delete(path, headers=AUTH)).status_code == 204
        assert (await client.get(path, headers=AUTH)).status_code == 404
        assert (await client.get(path + "/tasks", headers=AUTH)).status_code == 404
        assert (await client.get(path + "/documents", headers=AUTH)).status_code == 404
        assert (await client.get("/workbench/projects", headers=AUTH)).json()["total"] == 0
        assert (await client.delete("/workbench/projects/missing", headers=AUTH)).status_code == 404
