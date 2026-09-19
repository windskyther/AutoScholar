import base64
import binascii
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from autoscholar.agent.records import (
    ArtifactRecord,
    ExperimentRecord,
    ExperimentStatus,
    ToolCallStatus,
    ToolTraceRecord,
)
from autoscholar.coding.agent import CodingResult
from autoscholar.coding.errors import ErrorParser
from autoscholar.coding.sandbox import (
    SandboxArtifact,
    SandboxExecutor,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.experiment.analysis import analyze_experiment
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification, RawExperimentMetrics


class ExperimentRunError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ExperimentStore(Protocol):
    async def create_experiment(
        self,
        *,
        task_id: str,
        name: str,
        specification: dict[str, Any],
        dataset_id: str | None = None,
        dataset_sha256: str | None = None,
    ) -> ExperimentRecord: ...

    async def update_experiment(
        self,
        experiment_id: str,
        *,
        status: ExperimentStatus,
        metrics: dict[str, Any] | None = None,
        source_sha256: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ExperimentRecord: ...

    async def add_tool_call(
        self,
        *,
        task_id: str,
        sequence: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        status: ToolCallStatus,
        duration_ms: float,
        error_code: str | None = None,
    ) -> ToolTraceRecord: ...


class CodeRepair(Protocol):
    async def run(
        self,
        *,
        task_id: str,
        objective: str,
        plan: list[str],
        prior_traces: list[ToolTraceRecord] | None = None,
    ) -> CodingResult: ...


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    answer: str
    model: str
    traces: list[ToolTraceRecord]
    model_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    sandbox_runs: int
    repair_attempts: int
    files_written: int
    experiments_started: int
    experiments_succeeded: int
    training_runs: int
    artifact_count: int
    artifact_bytes: int
    experiment_duration_ms: int


class ExperimentService:
    """Bounded ML experiment workflow using trusted source templates and isolated Docker."""

    required_paths = (
        "outputs/raw_metrics.json",
        "outputs/loss.png",
        "outputs/accuracy.png",
        "checkpoints/mlp.pt",
        "checkpoints/cnn.pt",
    )

    def __init__(
        self,
        *,
        repository: ExperimentStore,
        workspace: WorkspaceManager,
        sandbox: SandboxExecutor,
        artifacts: ArtifactManager,
        repair: CodeRepair | None = None,
        timeout_seconds: int = 600,
        max_repairs: int = 3,
    ) -> None:
        self._repository = repository
        self._workspace = workspace
        self._sandbox = sandbox
        self._artifacts = artifacts
        self._repair = repair
        self._timeout = timeout_seconds
        self._max_repairs = max_repairs

    async def run(
        self,
        *,
        task_id: str,
        objective: str,
        plan: list[str],
        specification: ExperimentSpecification | None = None,
    ) -> ExperimentResult:
        started = time.perf_counter()
        spec = specification or ExperimentSpecification()
        health = await self._sandbox.health()
        if (
            health.status != "ok"
            or not health.mnist_dataset
            or health.dataset_id != "mnist"
            or not health.dataset_sha256
        ):
            raise ExperimentRunError(
                "experiment_dataset_unavailable",
                "The verified MNIST dataset or isolated sandbox is unavailable",
                status_code=503,
            )
        self._workspace.initialize(task_id)
        experiment = await self._repository.create_experiment(
            task_id=task_id,
            name=spec.name,
            specification=spec.model_dump(),
            dataset_id=health.dataset_id,
            dataset_sha256=health.dataset_sha256,
        )
        traces: list[ToolTraceRecord] = []
        sandbox_runs = 0
        repair_attempts = 0
        files_written = 0
        model_calls = input_tokens = output_tokens = total_tokens = 0
        model = "trusted-experiment-template"
        training_runs = 0
        saved: list[ArtifactRecord] = []
        try:
            await self._repository.update_experiment(experiment.id, status="preparing")
            template_root = Path(__file__).parent
            templates = {
                "train.py": (template_root / "train_template.py").read_text(encoding="utf-8"),
                "test_models.py": (template_root / "test_template.py").read_text(
                    encoding="utf-8"
                ),
                "experiment_config.json": json.dumps(
                    spec.model_dump(), ensure_ascii=False, indent=2
                ),
            }
            for path, content in templates.items():
                self._workspace.write_text(task_id, path, content)
            files_written += len(templates)
            source = self._workspace.source_snapshot(task_id)
            source_sha = hashlib.sha256(
                json.dumps(source, sort_keys=True).encode("utf-8")
            ).hexdigest()
            for action in ("static_check", "run_pytest", "run_python"):
                while True:
                    if action == "run_python":
                        await self._repository.update_experiment(
                            experiment.id, status="running", source_sha256=source_sha
                        )
                        training_runs += 1
                    request = SandboxRunRequest(
                        task_id=task_id,
                        action=action,
                        path="train.py" if action == "run_python" else None,
                        files=source,
                        collect_artifacts=(
                            list(self.required_paths) if action == "run_python" else []
                        ),
                        timeout_seconds=(
                            self._timeout if action == "run_python" else min(self._timeout, 120)
                        ),
                    )
                    result = await self._sandbox.run(request)
                    sandbox_runs += 1
                    traces.append(await self._record(task_id, traces, action, result))
                    if result.status == "succeeded":
                        break
                    diagnostic = ErrorParser.parse(result)
                    if self._repair is None or repair_attempts >= self._max_repairs:
                        raise ExperimentRunError(
                            diagnostic.category,
                            f"Experiment {action} failed; see persisted tool trace",
                        )
                    repair_attempts += 1
                    repaired = await self._repair.run(
                        task_id=task_id,
                        objective=(
                            f"Repair the existing MNIST MLP/CNN experiment. "
                            "Do not change experiment_config.json. "
                            f"Original objective: {objective}. "
                            f"The {action} step failed with {diagnostic.category}. "
                            "Reproduce with sandbox tools, then make the smallest safe repair."
                        ),
                        plan=[*plan, "Repair code and rerun validation"],
                        prior_traces=traces,
                    )
                    traces = repaired.traces
                    sandbox_runs += repaired.sandbox_runs
                    files_written += repaired.files_written
                    model_calls += repaired.model_calls
                    input_tokens += repaired.input_tokens
                    output_tokens += repaired.output_tokens
                    total_tokens += repaired.total_tokens
                    repair_attempts += repaired.repair_attempts
                    model = repaired.model
                    source = self._workspace.source_snapshot(task_id)
                    source_sha = hashlib.sha256(
                        json.dumps(source, sort_keys=True).encode("utf-8")
                    ).hexdigest()
            if {item.path for item in result.artifacts} != set(self.required_paths):
                raise ExperimentRunError(
                    "experiment_artifacts_missing",
                    "Training completed without the required experiment artifacts",
                )
            raw_artifact = next(
                item for item in result.artifacts if item.path == "outputs/raw_metrics.json"
            )
            raw_data = self._decode(raw_artifact)
            try:
                raw_metrics = RawExperimentMetrics.model_validate_json(raw_data)
            except ValidationError as exc:
                raise ExperimentRunError(
                    "experiment_metrics_invalid",
                    "Raw metrics artifact does not match the expected schema",
                ) from exc
            analysis = analyze_experiment(spec, raw_metrics)
            await self._repository.update_experiment(
                experiment.id, status="analyzing", metrics=analysis.metrics
            )
            for artifact in result.artifacts:
                saved.append(await self._artifacts.save_collected(task_id, experiment.id, artifact))
            generated: dict[str, bytes] = {
                "outputs/experiment.json": (
                    json.dumps(
                        {
                            "experiment_id": experiment.id,
                            "specification": spec.model_dump(),
                            "source_sha256": source_sha,
                            "dataset_sha256": health.dataset_sha256,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8"),
                "outputs/metrics.json": (
                    json.dumps(analysis.metrics, ensure_ascii=False, indent=2) + "\n"
                ).encode("utf-8"),
                "reports/report.md": analysis.report.encode("utf-8"),
                "logs/stdout.log": (result.stdout or "Experiment completed\n").encode("utf-8"),
                "logs/stderr.log": (result.stderr or "No stderr output\n").encode("utf-8"),
            }
            for path, artifact_content in generated.items():
                saved.append(
                    await self._artifacts.save(
                        task_id, experiment.id, path, artifact_content
                    )
                )
            await self._repository.update_experiment(
                experiment.id, status="succeeded", metrics=analysis.metrics
            )
            return ExperimentResult(
                answer=analysis.report,
                model=model,
                traces=traces,
                model_calls=model_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                sandbox_runs=sandbox_runs,
                repair_attempts=repair_attempts,
                files_written=files_written,
                experiments_started=1,
                experiments_succeeded=1,
                training_runs=training_runs,
                artifact_count=len(saved),
                artifact_bytes=sum(item.size_bytes for item in saved),
                experiment_duration_ms=round((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "experiment_run_failed"))
            message = str(getattr(exc, "message", "Experiment execution failed"))
            await self._repository.update_experiment(
                experiment.id, status="failed", error_code=code, error_message=message
            )
            raise

    async def _record(
        self,
        task_id: str,
        previous: list[ToolTraceRecord],
        action: str,
        result: SandboxRunResult,
    ) -> ToolTraceRecord:
        output = json.dumps(
            {
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "artifacts": [item.path for item in result.artifacts],
            },
            ensure_ascii=False,
        )
        return await self._repository.add_tool_call(
            task_id=task_id,
            sequence=len(previous) + 1,
            call_id=f"experiment-{len(previous) + 1}",
            tool_name=action,
            arguments={},
            output=output,
            status="succeeded" if result.status == "succeeded" else "failed",
            error_code=(
                None if result.status == "succeeded" else ErrorParser.parse(result).category
            ),
            duration_ms=result.duration_ms,
        )

    @staticmethod
    def _decode(artifact: SandboxArtifact) -> bytes:
        try:
            content = base64.b64decode(artifact.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ExperimentRunError(
                "experiment_metrics_invalid", "Raw metrics artifact encoding is invalid"
            ) from exc
        if (
            len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise ExperimentRunError(
                "experiment_metrics_invalid", "Raw metrics artifact integrity failed"
            )
        return content
