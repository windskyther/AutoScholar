"""Decode one unambiguous JSON object; never salvage part of a tool response."""

import json
import math
from typing import Any


class ToolArgumentsError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToolArgumentsError("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ToolArgumentsError("non_finite_number")


def _float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ToolArgumentsError("non_finite_number")
    return value


def decode_tool_arguments(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ToolArgumentsError("invalid_json")
    try:
        value = json.loads(
            raw, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
        )
    except json.JSONDecodeError as exc:
        reason = "extra_data" if exc.msg == "Extra data" else "invalid_json"
        raise ToolArgumentsError(reason) from exc
    except ToolArgumentsError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ToolArgumentsError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ToolArgumentsError("not_object")
    return value
