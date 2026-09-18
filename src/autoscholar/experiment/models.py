import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelName = Literal["mlp", "cnn"]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _default_models() -> list[ModelName]:
    return ["mlp", "cnn"]


class ExperimentSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    name: str = Field(default="mnist-mlp-vs-cnn", min_length=1, max_length=200)
    dataset: Literal["mnist"] = "mnist"
    models: list[ModelName] = Field(default_factory=_default_models)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    epochs: int = Field(default=2, ge=1, le=10)
    batch_size: int = Field(default=64, ge=8, le=256)
    learning_rate: FiniteFloat = Field(default=0.001, gt=0, le=1)
    train_samples: int = Field(default=2_048, ge=128, le=60_000)
    test_samples: int = Field(default=1_024, ge=128, le=10_000)
    primary_metric: Literal["test_accuracy"] = "test_accuracy"
    device: Literal["cpu"] = "cpu"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,199}", normalized):
            raise ValueError("experiment name contains unsupported characters")
        return normalized

    @model_validator(mode="after")
    def validate_comparison(self) -> "ExperimentSpecification":
        if self.models != ["mlp", "cnn"]:
            raise ValueError("Phase 5 requires the ordered model comparison: mlp, cnn")
        if not math.isfinite(self.learning_rate):
            raise ValueError("learning_rate must be finite")
        return self


class ModelMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelName
    train_loss: list[FiniteFloat] = Field(min_length=1, max_length=10)
    train_accuracy: list[FiniteFloat] = Field(min_length=1, max_length=10)
    test_accuracy: FiniteFloat = Field(ge=0, le=1)
    parameters: int = Field(ge=1)
    duration_seconds: FiniteFloat = Field(ge=0)

    @model_validator(mode="after")
    def validate_series(self) -> "ModelMetrics":
        if len(self.train_loss) != len(self.train_accuracy):
            raise ValueError("loss and accuracy series must have equal length")
        if any(value < 0 for value in self.train_loss):
            raise ValueError("training loss must be non-negative")
        if any(value < 0 or value > 1 for value in self.train_accuracy):
            raise ValueError("training accuracy must be between zero and one")
        return self


class RawExperimentMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    dataset: Literal["mnist"]
    seed: int = Field(ge=0, le=2**32 - 1)
    train_samples: int = Field(ge=1, le=60_000)
    test_samples: int = Field(ge=1, le=10_000)
    runs: list[ModelMetrics] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_runs(self) -> "RawExperimentMetrics":
        if [run.model for run in self.runs] != ["mlp", "cnn"]:
            raise ValueError("metrics must contain ordered mlp and cnn runs")
        return self
