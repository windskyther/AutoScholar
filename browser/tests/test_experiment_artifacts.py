import base64
import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.sandbox import SandboxArtifact
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.experiment.artifacts import ArtifactError, ArtifactManager
from autoscholar.rag import database_models as rag_database_models  # noqa: F401


async def _setup(tmp_path: Path) -> tuple[ArtifactManager, AgentTaskRepository, AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.create_task(task_id="task-a", objective="Compare models")
    await repository.create_task(task_id="task-b", objective="Other task")
    workspace = WorkspaceManager(tmp_path)
    workspace.initialize("task-a")
    workspace.initialize("task-b")
    return ArtifactManager(workspace, repository), repository, engine


async def test_artifact_store_validates_digest_and_task_scope(tmp_path: Path) -> None:
    manager, repository, engine = await _setup(tmp_path)
    experiment = await repository.create_experiment(
        task_id="task-a", name="comparison", specification={"dataset": "mnist"}
    )
    data = b'{"dataset":"mnist"}'
    collected = SandboxArtifact(
        path="outputs/raw_metrics.json",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data_base64=base64.b64encode(data).decode("ascii"),
    )

    record = await manager.save_collected("task-a", experiment.id, collected)
    assert manager.read_verified("task-a", record) == data
    assert record.path == "outputs/raw_metrics.json"

    with pytest.raises(ArtifactError, match="does not belong"):
        manager.read_verified("task-b", record)
    with pytest.raises(ArtifactError, match="does not belong"):
        await manager.save("task-b", experiment.id, "outputs/metrics.json", data)

    invalid = collected.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ArtifactError, match="digest"):
        await manager.save_collected("task-a", experiment.id, invalid)
    await engine.dispose()


async def test_artifact_store_checks_png_and_rejects_unregistered_paths(tmp_path: Path) -> None:
    manager, repository, engine = await _setup(tmp_path)
    experiment = await repository.create_experiment(
        task_id="task-a", name="comparison", specification={"dataset": "mnist"}
    )
    image = Image.new("RGB", (16, 16), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")

    record = await manager.save("task-a", experiment.id, "outputs/loss.png", output.getvalue())
    assert record.media_type == "image/png"

    with pytest.raises(ArtifactError, match="PNG"):
        await manager.save("task-a", experiment.id, "outputs/accuracy.png", b"not png")
    with pytest.raises(ArtifactError, match="not registered"):
        await manager.save("task-a", experiment.id, "outputs/../secret", b"bad")
    await engine.dispose()
