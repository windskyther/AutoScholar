"""Actual bounded MNIST experiment execution, persisted artifact recheck and checkpoint oracle."""

import base64
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.sandbox import SandboxExecutor, SandboxRunRequest
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.evaluation.component_fixture import QueryInputs, VersionedFixture, fixture_usage
from autoscholar.evaluation.datasets import decode_json
from autoscholar.evaluation.isolated_components import (
    ObservedSandbox,
    local_component_task,
    oracle_json,
)
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    FiniteNumber,
    Identifier,
    Observation,
    ScoreCard,
)
from autoscholar.experiment.analysis import ExperimentMetricsError
from autoscholar.experiment.artifacts import ArtifactError, ArtifactManager
from autoscholar.experiment.models import ExperimentSpecification, RawExperimentMetrics
from autoscholar.experiment.service import ExperimentRunError, ExperimentService

ExperimentStatus = Literal["accepted", "service_rejected", "oracle_rejected"]
ExperimentError = Literal[
    "experiment_metrics_invalid", "checkpoint_mismatch", "artifact_integrity_failed"
]


class BoundedExperimentSpecification(ExperimentSpecification):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    epochs: int = Field(default=1, ge=1, le=2)
    train_samples: int = Field(default=128, ge=128, le=512)
    test_samples: int = Field(default=128, ge=128, le=512)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Experiment schema version must be an integer")
        return value


class ExperimentScript(EvaluationModel):
    prompt: str = Field(min_length=1, max_length=10000)
    specification: BoundedExperimentSpecification
    fault: Literal["none", "invalid_metrics", "wrong_seed", "tampered_accuracy"] = "none"


class ExperimentFixture(VersionedFixture):
    scripts: dict[Identifier, ExperimentScript] = Field(min_length=1, max_length=100)


class VerifiedModel(EvaluationModel):
    model: Literal["mlp", "cnn"]
    accuracy: FiniteNumber = Field(ge=0, le=1)
    parameters: int = Field(ge=1)
    verified: bool


class CheckpointOracle(EvaluationModel):
    schema_version: int = Field(ge=1, le=1)
    dataset_verified: bool
    plots_verified: bool
    models: list[VerifiedModel] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def ordered_models(self) -> "CheckpointOracle":
        if [model.model for model in self.models] != ["mlp", "cnn"]:
            raise ValueError("Checkpoint oracle must verify each ordered model exactly once")
        return self


class ExperimentLabels(EvaluationModel):
    status: ExperimentStatus
    error_code: ExperimentError | None = None
    artifact_count: int = Field(ge=0, le=10)

    @model_validator(mode="after")
    def coherent(self) -> "ExperimentLabels":
        if (self.status == "accepted") != (self.error_code is None):
            raise ValueError("Experiment rejection needs a specific error code")
        return self


class ExperimentObservation(EvaluationModel):
    status: ExperimentStatus
    error_code: ExperimentError | None = None
    service_completed: bool
    metric_extraction_valid: bool
    artifact_set_valid: bool
    artifact_integrity_valid: bool
    artifact_count: int = Field(ge=0, le=10)
    verified_artifact_count: int = Field(ge=0, le=10)
    oracle: CheckpointOracle | None = None
    sandbox_runs: int = Field(ge=0, le=5)


def score_experiment(observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
    labels = ExperimentLabels.model_validate(expected)
    actual = ExperimentObservation.model_validate(observation.payload)
    checkpoints = (
        None if actual.oracle is None else all(item.verified for item in actual.oracle.models)
    )
    checks = {
        "experiment_outcome": actual.status == labels.status,
        "error_classification": actual.error_code == labels.error_code,
        "artifact_count": actual.artifact_count == labels.artifact_count,
    }
    if labels.status == "accepted":
        checks.update(
            {
                "service_completed": actual.service_completed,
                "metric_extraction": actual.metric_extraction_valid,
                "required_artifacts": actual.artifact_set_valid,
                "physical_artifact_integrity": actual.artifact_integrity_valid,
                "checkpoints_recomputed": checkpoints is True,
                "dataset_snapshot": actual.oracle is not None and actual.oracle.dataset_verified,
                "plots_decodable": actual.oracle is not None and actual.oracle.plots_verified,
            }
        )
    elif labels.status == "oracle_rejected":
        checks["independent_failure_detected"] = actual.service_completed and (
            not actual.artifact_integrity_valid
            if labels.error_code == "artifact_integrity_failed"
            else actual.oracle is not None
            and (
                not actual.metric_extraction_valid
                or not actual.oracle.dataset_verified
                or not actual.oracle.plots_verified
                or checkpoints is False
            )
        )
    else:
        checks["no_successful_artifacts_published"] = (
            not actual.service_completed and actual.artifact_count == 0
        )
    accepted = (
        actual.status == "accepted"
        and actual.service_completed
        and actual.metric_extraction_valid
        and actual.artifact_set_valid
        and actual.artifact_integrity_valid
        and checkpoints is True
        and actual.oracle is not None
        and actual.oracle.dataset_verified
        and actual.oracle.plots_verified
    )
    return ScoreCard(
        checks=checks,
        metrics={
            "experiment_acceptance": float(accepted),
            "service_completion": float(actual.service_completed),
            "metric_extraction_validity": float(actual.metric_extraction_valid),
            "artifact_integrity": float(actual.artifact_integrity_valid)
            if actual.artifact_count
            else None,
            "verified_artifacts": float(actual.verified_artifact_count),
            "checkpoint_correctness": float(checkpoints) if checkpoints is not None else None,
            "dataset_verification": float(actual.oracle.dataset_verified)
            if actual.oracle
            else None,
            "plot_verification": float(actual.oracle.plots_verified) if actual.oracle else None,
            "sandbox_runs": float(actual.sandbox_runs),
            "oracle_rejection": float(actual.status == "oracle_rejected"),
            "expected_rejection": float(labels.status != "accepted"),
        },
    )


def _oracle_files(
    contents: dict[str, bytes],
    specification: ExperimentSpecification,
    raw: RawExperimentMetrics,
    dataset_sha256: str,
    *,
    source: str,
    harness: str,
) -> dict[str, str]:
    files = {"checkpoint_models.py": source, "eval_oracle.py": harness}
    checkpoints: list[list[str]] = []
    for model in ("mlp", "cnn"):
        encoded = base64.b64encode(contents[f"checkpoints/{model}.pt"]).decode("ascii")
        chunks: list[str] = []
        for index, start in enumerate(range(0, len(encoded), 650000)):
            path = f"payloads/{model}-{index}.b64"
            files[path] = encoded[start : start + 650000]
            chunks.append(path)
        checkpoints.append(chunks)
    plots: list[str] = []
    for plot in ("loss", "accuracy"):
        path = f"payloads/{plot}.b64"
        files[path] = base64.b64encode(contents[f"outputs/{plot}.png"]).decode("ascii")
        plots.append(path)
    files["oracle_payload.json"] = json.dumps(
        {
            "specification": specification.model_dump(),
            "raw_metrics": raw.model_dump(),
            "dataset_sha256": dataset_sha256,
            "checkpoints": checkpoints,
            "plots": plots,
        },
        allow_nan=False,
    )
    return files


class ExperimentVerification(EvaluationModel):
    metric_extraction_valid: bool = False
    artifact_set_valid: bool = False
    artifact_integrity_valid: bool = False
    artifact_count: int = Field(default=0, ge=0, le=10)
    verified_artifact_count: int = Field(default=0, ge=0, le=10)
    oracle: CheckpointOracle | None = None

    @property
    def accepted(self) -> bool:
        return (
            self.metric_extraction_valid
            and self.artifact_set_valid
            and self.artifact_integrity_valid
            and self.artifact_count == self.verified_artifact_count == len(ArtifactManager.allowed)
            and self.oracle is not None
            and self.oracle.dataset_verified
            and self.oracle.plots_verified
            and all(item.verified for item in self.oracle.models)
        )


async def verify_completed_experiment(
    *,
    task_id: str,
    store: AgentTaskRepository,
    workspace: WorkspaceManager,
    sandbox: SandboxExecutor,
    resources: dict[str, str],
    specification: ExperimentSpecification,
    train_source: str,
    oracle_source: str,
) -> ExperimentVerification:
    """Re-read saved artifacts and grade in a separate sandbox, outside task budgets."""
    records = await store.list_artifacts(task_id)
    artifacts = ArtifactManager(workspace, store)
    contents: dict[str, bytes] = {}
    for record in records:
        try:
            contents[record.path] = artifacts.read_verified(task_id, record)
        except ArtifactError:
            continue
    set_valid = (
        len(records) == len(ArtifactManager.allowed)
        and {record.path for record in records} == ArtifactManager.allowed.keys()
    )
    verified = ExperimentVerification(
        artifact_count=len(records),
        verified_artifact_count=len(contents),
        artifact_set_valid=set_valid,
        artifact_integrity_valid=len(contents) == len(records) and set_valid,
    )
    if not verified.artifact_integrity_valid:
        return verified
    raw = RawExperimentMetrics.model_validate(decode_json(contents["outputs/raw_metrics.json"]))
    metrics = decode_json(contents["outputs/metrics.json"])
    manifest = decode_json(contents["outputs/experiment.json"])
    experiments = await store.list_experiments(task_id)
    delta = raw.runs[1].test_accuracy - raw.runs[0].test_accuracy
    winner = "cnn" if delta > 0 else "mlp" if delta < 0 else "tie"
    metadata_valid = (
        raw.dataset == specification.dataset
        and raw.seed == specification.seed
        and raw.train_samples == specification.train_samples
        and raw.test_samples == specification.test_samples
        and all(len(run.train_loss) == specification.epochs for run in raw.runs)
    )
    source_sha = hashlib.sha256(
        json.dumps(workspace.source_snapshot(task_id), sort_keys=True).encode()
    ).hexdigest()
    report = contents["reports/report.md"].decode("utf-8")
    verified.metric_extraction_valid = (
        isinstance(metrics, dict)
        and metadata_valid
        and all(
            metrics.get(key) == specification.model_dump()[key]
            for key in (
                "dataset",
                "seed",
                "train_samples",
                "test_samples",
                "epochs",
                "batch_size",
                "learning_rate",
            )
        )
        and metrics.get("winner") == winner
        and metrics.get("cnn_minus_mlp_accuracy") == delta
        and metrics.get("runs") == [run.model_dump() for run in raw.runs]
        and len(experiments) == 1
        and experiments[0].metrics == metrics
        and experiments[0].specification == specification.model_dump()
        and experiments[0].source_sha256 == source_sha
        and experiments[0].dataset_sha256 == resources["dataset"]
        and decode_json(workspace.read_text(task_id, "experiment_config.json").encode())
        == specification.model_dump()
        and isinstance(manifest, dict)
        and manifest.get("specification") == specification.model_dump()
        and manifest.get("source_sha256") == source_sha
        and manifest.get("dataset_sha256") == resources["dataset"]
        and all(f"{run.test_accuracy:.2%}" in report for run in raw.runs)
    )
    checked = await sandbox.run(
        SandboxRunRequest(
            task_id=task_id,
            action="run_python",
            path="eval_oracle.py",
            timeout_seconds=60,
            files=_oracle_files(
                contents,
                specification,
                raw,
                resources["dataset"],
                source=train_source,
                harness=oracle_source,
            ),
            collect_artifacts=["outputs/eval_oracle.json"],
        )
    )
    verified.oracle = CheckpointOracle.model_validate(oracle_json(checked))
    return verified


class InjectedExperimentAdapter:
    def __init__(
        self,
        fixture: ExperimentFixture,
        *,
        fixture_sha256: str,
        sandbox: SandboxExecutor,
        resources: dict[str, str],
        workspace_root: Path,
    ) -> None:
        self.fixture = fixture
        self.sandbox = sandbox
        self.resources = resources
        self.workspace_root = workspace_root
        template_root = Path(__file__).resolve().parents[1] / "experiment"
        self.train_source = (template_root / "train_template.py").read_text(encoding="utf-8")
        self.test_source = (template_root / "test_template.py").read_text(encoding="utf-8")
        self.oracle_source = (
            Path(__file__).with_name("experiment_oracle.py").read_text(encoding="utf-8")
        )
        self.identity = AdapterIdentity(
            name="isolated-mnist-experiment",
            category="experiment",
            variant="baseline",
            execution="injected",
            resources={
                **resources,
                "experiment_fixture": fixture_sha256,
                "train_template": hashlib.sha256(self.train_source.encode()).hexdigest(),
                "test_template": hashlib.sha256(self.test_source.encode()).hexdigest(),
                "checkpoint_oracle": hashlib.sha256(self.oracle_source.encode()).hexdigest(),
            },
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = QueryInputs.model_validate(case.inputs)
        ExperimentLabels.model_validate(case.expected)
        script = self.fixture.scripts.get(query.query_id)
        if script is None or script.prompt != case.prompt:
            raise ValueError("Experiment fixture input binding differs")

    def _sources(self, script: ExperimentScript) -> dict[str, str]:
        source = self.train_source
        if script.fault != "none":
            mutation = {
                "invalid_metrics": "    measured['runs'] = []\n",
                "wrong_seed": "    measured['seed'] += 1\n",
                "tampered_accuracy": (
                    "    measured['runs'][0]['test_accuracy'] = 0.123456\n"
                    "    measured['runs'][1]['test_accuracy'] = 0.987654\n"
                ),
            }[script.fault]
            source += (
                "\nif __name__ == '__main__':\n"
                "    raw_path = Path('outputs/raw_metrics.json')\n"
                "    measured = json.loads(raw_path.read_text(encoding='utf-8'))\n"
                + mutation
                + "    raw_path.write_text(json.dumps(measured), encoding='utf-8')\n"
            )
        return {"train.py": source, "test_models.py": self.test_source}

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = QueryInputs.model_validate(inputs)
        script = self.fixture.scripts[query.query_id]
        if script.prompt != prompt:
            raise ValueError("Experiment input changed after preflight")
        sandbox = ObservedSandbox(self.sandbox, self.resources)
        oracle: CheckpointOracle | None = None
        completed = False
        extraction = set_valid = integrity = False
        verified_count = 0
        status: ExperimentStatus = "service_rejected"
        error: ExperimentError | None = "experiment_metrics_invalid"
        async with local_component_task(self.workspace_root, prompt, mode="experiment") as (
            task_id,
            store,
            workspace,
        ):
            artifacts = ArtifactManager(workspace, store)
            service = ExperimentService(
                repository=store,
                workspace=workspace,
                sandbox=sandbox,
                artifacts=artifacts,
                repair=None,
                timeout_seconds=60,
                max_repairs=0,
            )
            try:
                await service.run(
                    task_id=task_id,
                    objective=prompt,
                    plan=["Execute bounded public MNIST comparison"],
                    specification=script.specification,
                    source_files=self._sources(script),
                )
                completed = True
            except (ExperimentRunError, ExperimentMetricsError) as exc:
                if exc.code != "experiment_metrics_invalid":
                    raise
            records = await store.list_artifacts(task_id)
            if completed:
                verified = await verify_completed_experiment(
                    task_id=task_id,
                    store=store,
                    workspace=workspace,
                    sandbox=sandbox,
                    resources=self.resources,
                    specification=script.specification,
                    train_source=self.train_source,
                    oracle_source=self.oracle_source,
                )
                extraction = verified.metric_extraction_valid
                set_valid = verified.artifact_set_valid
                integrity = verified.artifact_integrity_valid
                verified_count = verified.verified_artifact_count
                oracle = verified.oracle
                status = "accepted" if verified.accepted else "oracle_rejected"
                error = (
                    None
                    if verified.accepted
                    else ("checkpoint_mismatch" if integrity else "artifact_integrity_failed")
                )
            actual = ExperimentObservation(
                status=status,
                error_code=error,
                service_completed=completed,
                metric_extraction_valid=extraction,
                artifact_set_valid=set_valid,
                artifact_integrity_valid=integrity,
                artifact_count=len(records),
                verified_artifact_count=verified_count,
                oracle=oracle,
                sandbox_runs=len(sandbox.runs),
            )
            return Observation(payload=actual.model_dump(mode="json"), usage=fixture_usage())

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_experiment(observation, expected)
