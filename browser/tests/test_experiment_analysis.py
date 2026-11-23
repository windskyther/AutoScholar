import pytest

from autoscholar.experiment.analysis import ExperimentMetricsError, analyze_experiment
from autoscholar.experiment.models import ExperimentSpecification, RawExperimentMetrics


def _raw() -> RawExperimentMetrics:
    return RawExperimentMetrics.model_validate(
        {
            "dataset": "mnist",
            "seed": 42,
            "train_samples": 128,
            "test_samples": 128,
            "runs": [
                {
                    "model": "mlp",
                    "train_loss": [1.5],
                    "train_accuracy": [0.4],
                    "test_accuracy": 0.5,
                    "parameters": 100,
                    "duration_seconds": 2.0,
                },
                {
                    "model": "cnn",
                    "train_loss": [1.2],
                    "train_accuracy": [0.6],
                    "test_accuracy": 0.75,
                    "parameters": 200,
                    "duration_seconds": 3.0,
                },
            ],
        }
    )


def test_analysis_uses_only_validated_measured_values() -> None:
    specification = ExperimentSpecification(
        epochs=1, train_samples=128, test_samples=128
    )
    result = analyze_experiment(specification, _raw())

    assert result.metrics["winner"] == "cnn"
    assert result.metrics["cnn_minus_mlp_accuracy"] == 0.25
    assert "75.00%" in result.report
    assert "工程验收" in result.report


def test_analysis_rejects_mismatched_reproducibility_fields() -> None:
    specification = ExperimentSpecification(
        epochs=2, train_samples=128, test_samples=128
    )
    with pytest.raises(ExperimentMetricsError):
        analyze_experiment(specification, _raw())
