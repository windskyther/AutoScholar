"""Versioned evaluation contracts, independent of application settings and providers."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Category = Literal["rag", "research", "tool", "coding", "experiment", "end_to_end"]
CaseStatus = Literal["passed", "failed", "timeout", "error", "skipped", "cancelled"]
CaseReason = Literal["checks_failed", "timeout", "adapter_error", "cancelled"]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BenchmarkCase(EvaluationModel):
    id: Identifier
    prompt: str = Field(min_length=1, max_length=10000)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    expected: dict[str, JsonValue]

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must not be blank")
        return value


class BenchmarkSuite(EvaluationModel):
    schema_version: Literal[1]
    id: Identifier
    category: Category
    description: str = Field(min_length=1, max_length=2000)
    provenance: str = Field(min_length=1, max_length=2000)
    cases: list[BenchmarkCase] = Field(min_length=1, max_length=1000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_cases(self) -> "BenchmarkSuite":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Benchmark case IDs must be unique")
        return self


class RunConfiguration(EvaluationModel):
    profile: Literal["offline", "injected"] = "offline"
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    repeats: int = Field(default=1, ge=1, le=10)
    case_timeout_seconds: FiniteNumber = Field(default=30.0, gt=0, le=600)


class AdapterIdentity(EvaluationModel):
    name: Identifier
    category: Category
    variant: Identifier
    execution: Literal["fixture", "injected"]
    model: str | None = Field(default=None, max_length=200)
    # Input-file digests, not paths, URLs, credentials, or arbitrary provider settings.
    resources: dict[Identifier, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = Field(
        default_factory=dict
    )


class MeasuredUsage(EvaluationModel):
    model_calls: int | None = Field(default=None, ge=0)
    external_api_calls: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    budget_tokens: int | None = Field(default=None, ge=0)
    monetary_cost: FiniteNumber | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def token_totals(self) -> "MeasuredUsage":
        if (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("Measured token total must equal input plus output")
        return self


class Observation(EvaluationModel):
    payload: dict[str, JsonValue]
    usage: MeasuredUsage = Field(default_factory=MeasuredUsage)


class ScoreCard(EvaluationModel):
    checks: dict[Identifier, bool] = Field(min_length=1, max_length=100)
    metrics: dict[Identifier, FiniteNumber | None] = Field(default_factory=dict, max_length=200)
    # Only E2E adapters may score whether the user's actual task was accomplished.
    task_success: bool | None = None


class CaseResult(EvaluationModel):
    case_id: Identifier
    category: Category
    adapter: AdapterIdentity
    repeat: int = Field(ge=1, le=10)
    seed: int = Field(ge=0, le=2**32 - 1)
    status: CaseStatus
    reason: CaseReason | None = None
    duration_ms: FiniteNumber = Field(ge=0)
    score: ScoreCard | None = None
    usage: MeasuredUsage = Field(default_factory=MeasuredUsage)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunManifest(EvaluationModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^eval-[a-f0-9]{32}$")
    started_at: str
    completed_at: str | None = None
    suite_id: Identifier
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration: RunConfiguration
    adapters: list[AdapterIdentity] = Field(min_length=1, max_length=10)
    git_revision: str | None
    git_dirty: bool | None
    python_version: str
    platform: str
    state: Literal["running", "completed", "cancelled", "error"] = "running"
