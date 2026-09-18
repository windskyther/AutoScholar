import math

import pytest
from pydantic import ValidationError

from autoscholar.experiment import ExperimentSpecification, RawExperimentMetrics


def test_experiment_specification_has_reproducible_bounded_defaults() -> None:
    specification = ExperimentSpecification()

    assert specification.models == ["mlp", "cnn"]
    assert specification.seed == 42
    assert specification.device == "cpu"
    assert specification.train_samples < 60_000


def test_experiment_specification_rejects_uncontrolled_variants() -> None:
    with pytest.raises(ValidationError):
        ExperimentSpecification(models=["cnn", "mlp"])
    with pytest.raises(ValidationError):
        ExperimentSpecification(epochs=100)
    with pytest.raises(ValidationError):
        ExperimentSpecification(learning_rate=math.nan)


def test_raw_metrics_require_ordered_finite_model_results() -> None:
    metrics = RawExperimentMetrics.model_validate(
        {
            "schema_version": 1,
            "dataset": "mnist",
            "seed": 42,
            "train_samples": 256,
            "test_samples": 128,
            "runs": [
                {
                    "model": "mlp",
                    "train_loss": [1.2],
                    "train_accuracy": [0.5],
                    "test_accuracy": 0.55,
                    "parameters": 100,
                    "duration_seconds": 1.0,
                },
                {
                    "model": "cnn",
                    "train_loss": [1.0],
                    "train_accuracy": [0.6],
                    "test_accuracy": 0.65,
                    "parameters": 200,
                    "duration_seconds": 2.0,
                },
            ],
        }
    )

    assert metrics.runs[1].model == "cnn"

    invalid = metrics.model_dump()
    invalid["runs"][0]["test_accuracy"] = math.inf
    with pytest.raises(ValidationError):
        RawExperimentMetrics.model_validate(invalid)
