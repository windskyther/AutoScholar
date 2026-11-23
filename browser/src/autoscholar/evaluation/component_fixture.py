"""Shared bounded contracts for public, controlled component fixtures."""

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import field_validator

from autoscholar.evaluation.datasets import decode_json, read_bounded
from autoscholar.evaluation.models import EvaluationModel, Identifier, MeasuredUsage


class VersionedFixture(EvaluationModel):
    schema_version: Literal[1]
    suite_id: Identifier

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Fixture version must be an integer")
        return value


class QueryInputs(EvaluationModel):
    query_id: Identifier


def load_fixture[Model: EvaluationModel](path: Path, model: type[Model]) -> tuple[Model, str]:
    raw = read_bounded(path)
    return model.model_validate(decode_json(raw)), hashlib.sha256(raw).hexdigest()


def fixture_usage(*, model_calls: int = 0) -> MeasuredUsage:
    # Script callbacks are counted, but no vendor generated/tokenized any text.
    return MeasuredUsage(
        model_calls=model_calls,
        external_api_calls=0,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
    )
