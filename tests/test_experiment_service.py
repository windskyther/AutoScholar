import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.records import ToolTraceRecord
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.agent import CodingResult
from autoscholar.coding.sandbox import (
    SandboxArtifact,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification
from autoscholar.experiment.service import ExperimentRunError, ExperimentService
from autoscholar.rag import database_models as rag_database_models  # noqa: F401


def _artifact(path: str, content: bytes) -> SandboxArtifact:
    return SandboxArtifact(
        path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        data_base64=base64.b64encode(content).decode("ascii"),
    )


class FakeExperimentSandbox:
    def __init__(self) -> None:
        self.requests: list[SandboxRunRequest] = []

    async def health(self) -> SandboxHealth:
        return SandboxHealth(
            status="ok",
            engine=True,
            image=True,
            mnist_dataset=True,
            dataset_id="mnist",
            dataset_sha256="a" * 64,
        )

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        self.requests.append(request)
        if request.action != "run_python":
            return SandboxRunResult(
                status="succeeded", exit_code=0, stdout="ok", stderr="", duration_ms=10
            )
        config = json.loads(request.files["experiment_config.json"])
        raw = {
            "dataset": "mnist",
            "seed": config["seed"],
            "train_samples": config["train_samples"],
            "test_samples": config["test_samples"],
            "runs": [
                {
                    "model": model,
                    "train_loss": [1.0] * config["epochs"],
                    "train_accuracy": [0.5] * config["epochs"],
                    "test_accuracy": accuracy,
                    "parameters": 100,
                    "duration_seconds": 1.0,
                }
                for model, accuracy in (("mlp", 0.7), ("cnn", 0.8))
            ],
        }
        png_buffer = io.BytesIO()
        Image.new("RGB", (32, 32), "white").save(png_buffer, format="PNG")
        checkpoint_buffer = io.BytesIO()
        with zipfile.ZipFile(checkpoint_buffer, "w") as archive:
            archive.writestr("weights", b"fake")
        artifacts = [
            _artifact("outputs/raw_metrics.json", json.dumps(raw).encode("utf-8")),
            _artifact("outputs/loss.png", png_buffer.getvalue()),
            _artifact("outputs/accuracy.png", png_buffer.getvalue()),
            _artifact("checkpoints/mlp.pt", checkpoint_buffer.getvalue()),
            _artifact("checkpoints/cnn.pt", checkpoint_buffer.getvalue()),
        ]
        return SandboxRunResult(
            status="succeeded",
            exit_code=0,
            stdout="training done",
            stderr="",
            duration_ms=100,
            artifacts=artifacts,
        )

    async def close(self) -> None:
        return None


class FailOnceSandbox(FakeExperimentSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if not self.failed and request.action == "static_check":
            self.failed = True
            self.requests.append(request)
            return SandboxRunResult(
                status="failed",
                exit_code=1,
                stdout="",
                stderr="SyntaxError: test fault",
                duration_ms=10,
            )
        return await super().run(request)


class FakeRepair:
    def __init__(self, workspace: WorkspaceManager) -> None:
        self.workspace = workspace
        self.calls = 0

    async def run(
        self,
        *,
        task_id: str,
        objective: str,
        plan: list[str],
        prior_traces: list[ToolTraceRecord] | None = None,
    ) -> CodingResult:
        assert "static_check" in objective
        assert plan[-1] == "Repair code and rerun validation"
        self.calls += 1
        source = self.workspace.read_text(task_id, "train.py")
        self.workspace.write_text(
            task_id, "train.py", source + "\n# repaired\n", overwrite=True
        )
        return CodingResult(
            answer="Repaired",
            model="fake-repair",
            traces=list(prior_traces or []),
            model_calls=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            sandbox_runs=0,
            repair_attempts=0,
            files_written=1,
        )


async def test_experiment_service_runs_and_persists_comparison(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.create_task(task_id="experiment-task", objective="Compare MLP and CNN")
    workspace = WorkspaceManager(tmp_path)
    sandbox = FakeExperimentSandbox()
    service = ExperimentService(
        repository=repository,
        workspace=workspace,
        sandbox=sandbox,
        artifacts=ArtifactManager(workspace, repository),
    )

    result = await service.run(
        task_id="experiment-task",
        objective="Compare MLP and CNN",
        plan=["Train", "Analyze"],
        specification=ExperimentSpecification(
            epochs=1, train_samples=128, test_samples=128
        ),
    )

    assert result.experiments_succeeded == 1
    assert result.training_runs == 1
    assert result.sandbox_runs == 3
    assert result.artifact_count == 10
    assert [request.action for request in sandbox.requests] == [
        "static_check",
        "run_pytest",
        "run_python",
    ]
    experiments = await repository.list_experiments("experiment-task")
    assert experiments[0].status == "succeeded"
    assert experiments[0].metrics["winner"] == "cnn"
    assert len(await repository.list_artifacts("experiment-task")) == 10
    assert "75.00%" not in result.answer
    assert "80.00%" in result.answer
    await engine.dispose()


async def test_experiment_service_retries_failed_validation_and_preserves_traces(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.create_task(
        task_id="retry-task", objective="Compare MLP and CNN", mode="experiment"
    )
    workspace = WorkspaceManager(tmp_path)
    sandbox = FailOnceSandbox()
    repair = FakeRepair(workspace)
    service = ExperimentService(
        repository=repository,
        workspace=workspace,
        sandbox=sandbox,
        artifacts=ArtifactManager(workspace, repository),
        repair=repair,
        max_repairs=1,
    )

    result = await service.run(
        task_id="retry-task",
        objective="Compare MLP and CNN",
        plan=["Train", "Analyze"],
        specification=ExperimentSpecification(
            epochs=1, train_samples=128, test_samples=128
        ),
    )

    assert repair.calls == 1
    assert result.repair_attempts == 1
    assert result.sandbox_runs == 4
    assert [trace.sequence for trace in result.traces] == [1, 2, 3, 4]
    assert [request.action for request in sandbox.requests] == [
        "static_check",
        "static_check",
        "run_pytest",
        "run_python",
    ]
    assert "# repaired" in sandbox.requests[-1].files["train.py"]
    assert (await repository.list_experiments("retry-task"))[0].status == "succeeded"
    await engine.dispose()


async def test_experiment_service_persists_failed_validation(tmp_path: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
    await repository.create_task(
        task_id="failed-task", objective="Compare MLP and CNN", mode="experiment"
    )
    workspace = WorkspaceManager(tmp_path)
    service = ExperimentService(
        repository=repository,
        workspace=workspace,
        sandbox=FailOnceSandbox(),
        artifacts=ArtifactManager(workspace, repository),
        repair=None,
    )

    with pytest.raises(ExperimentRunError):
        await service.run(
            task_id="failed-task",
            objective="Compare MLP and CNN",
            plan=["Train", "Analyze"],
            specification=ExperimentSpecification(
                epochs=1, train_samples=128, test_samples=128
            ),
        )
    experiment = (await repository.list_experiments("failed-task"))[0]
    assert experiment.status == "failed"
    assert experiment.error_code
    assert await repository.list_artifacts("failed-task") == []
    await engine.dispose()
