from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.rag import database_models as rag_database_models  # noqa: F401


async def test_repository_persists_experiment_lifecycle_and_artifacts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.create_task(task_id="task-exp", objective="Compare MLP and CNN")

    experiment = await repository.create_experiment(
        task_id="task-exp",
        name="mnist-mlp-vs-cnn",
        specification={"dataset": "mnist", "models": ["mlp", "cnn"]},
        dataset_id="mnist",
        dataset_sha256="a" * 64,
    )
    running = await repository.update_experiment(
        experiment.id,
        status="running",
        source_sha256="b" * 64,
    )
    artifact = await repository.add_artifact(
        task_id="task-exp",
        experiment_id=experiment.id,
        artifact_type="metrics",
        path="outputs/metrics.json",
        media_type="application/json",
        size_bytes=100,
        sha256="c" * 64,
    )
    complete = await repository.update_experiment(
        experiment.id,
        status="succeeded",
        metrics={"winner": "cnn", "accuracy_delta": 0.1},
    )

    assert running.started_at is not None
    assert complete.finished_at is not None
    assert complete.metrics["winner"] == "cnn"
    assert await repository.get_experiment(experiment.id) == complete
    assert await repository.list_experiments("task-exp") == [complete]
    assert await repository.get_artifact(artifact.id) == artifact
    assert await repository.list_artifacts("task-exp") == [artifact]
    assert await repository.list_artifacts(
        "task-exp", experiment_id=experiment.id
    ) == [artifact]

    await engine.dispose()
