"""Run a small real MNIST comparison against the configured Docker sandbox."""

import asyncio
import json
from uuid import uuid4

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.sandbox import SandboxClient
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.config import Settings
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.experiment.service import ExperimentService
from autoscholar.infrastructure.database import Database
from autoscholar.rag import database_models as rag_database_models  # noqa: F401


async def run_smoke() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    sandbox = SandboxClient(
        settings.sandbox_manager_url,
        timeout_seconds=settings.experiment_timeout_seconds + 10,
    )
    task_id = str(uuid4())
    objective = "Compare MLP and CNN on a fixed MNIST subset"
    plan = ["Validate model code", "Train both models", "Compare measured results"]
    repository = AgentTaskRepository(database.session_factory)
    workspace = WorkspaceManager(
        settings.workspace_root,
        max_files=settings.workspace_max_files,
        max_file_bytes=settings.workspace_max_file_bytes,
        max_source_bytes=settings.workspace_max_source_bytes,
        max_artifact_files=settings.workspace_max_artifact_files,
        max_artifact_file_bytes=settings.workspace_max_artifact_file_bytes,
        max_artifact_bytes=settings.workspace_max_artifact_bytes,
    )
    artifacts = ArtifactManager(workspace, repository)
    service = ExperimentService(
        repository=repository,
        workspace=workspace,
        sandbox=sandbox,
        artifacts=artifacts,
        timeout_seconds=settings.experiment_timeout_seconds,
        repair=None,
    )
    try:
        await repository.create_task(
            task_id=task_id, objective=objective, mode="experiment"
        )
        try:
            result = await service.run(
                task_id=task_id,
                objective=objective,
                plan=plan,
                specification=ExperimentSpecification(
                    epochs=1, train_samples=128, test_samples=128
                ),
            )
        except Exception as exc:
            await repository.update_task(
                task_id,
                status="failed",
                plan=plan,
                answer=None,
                metrics={},
                error_code=str(getattr(exc, "code", "experiment_smoke_failed")),
                error_message="Experiment smoke test failed",
                mode="experiment",
            )
            raise
        await repository.update_task(
            task_id,
            status="succeeded",
            plan=plan,
            answer=result.answer,
            metrics={
                "sandbox_runs": result.sandbox_runs,
                "training_runs": result.training_runs,
                "artifact_count": result.artifact_count,
            },
            mode="experiment",
        )
        experiment = (await repository.list_experiments(task_id))[0]
        saved = await repository.list_artifacts(task_id)
        assert experiment.status == "succeeded"
        assert result.training_runs == 1
        assert len(saved) == 10
        assert all(artifacts.read_verified(task_id, item) for item in saved)
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "experiment_id": experiment.id,
                    "status": experiment.status,
                    "winner": experiment.metrics["winner"],
                    "test_accuracy": {
                        run["model"]: run["test_accuracy"]
                        for run in experiment.metrics["runs"]
                    },
                    "sandbox_runs": result.sandbox_runs,
                    "artifacts": len(saved),
                    "dataset_sha256": experiment.dataset_sha256,
                },
                ensure_ascii=True,
            )
        )
    finally:
        await sandbox.close()
        await database.close()


if __name__ == "__main__":
    asyncio.run(run_smoke())
