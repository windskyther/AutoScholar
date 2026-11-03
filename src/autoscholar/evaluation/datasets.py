"""Bounded UTF-8 JSON loading; never interpret dataset contents as code."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from autoscholar.evaluation.models import BenchmarkSuite

MAX_DATASET_BYTES = 8 * 1024 * 1024


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON number is not allowed")


def _number(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number is not allowed")
    return result


def decode_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8 JSON") from exc


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_DATASET_BYTES + 1)
    if not raw or len(raw) > MAX_DATASET_BYTES:
        raise ValueError("Dataset must be nonempty and at most 8 MiB")
    return raw


def load_suite(path: Path) -> tuple[BenchmarkSuite, str]:
    raw = read_bounded(path)
    return BenchmarkSuite.model_validate(decode_json(raw)), hashlib.sha256(raw).hexdigest()


def payload_digest(payload: object) -> str:
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
