"""Categorical error diagnostics: never persist arbitrary vendor prose or headers."""

from typing import Any

from autoscholar.evaluation.datasets import decode_json

TERMS = (
    "tool_choice",
    "thinking",
    "reasoning",
    "required",
    "strict",
    "schema",
    "minlength",
    "maxlength",
    "unsupported",
    "not supported",
    "invalid",
    "authentication",
    "api_key",
    "rate limit",
    "context",
    "max_tokens",
)
PARAMETERS = ("tool_choice", "thinking", "reasoning_effort", "tools", "max_tokens", "model")
ERROR_TYPES = (
    "invalid_request_error",
    "authentication_error",
    "rate_limit_error",
    "server_error",
)


def error_diagnostic(content: bytes, status: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "http_status": status,
        "body_format": "unrecognized",
        "error_type": None,
        "parameter": None,
        "reported_terms": [],
        "raw_body_omitted": True,
        "interpretation": "vendor_text_hints_only",
    }
    try:
        payload = decode_json(content)
    except ValueError:
        return result
    result["body_format"] = "json"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return result
    result["body_format"] = "error_object"
    if error.get("type") in ERROR_TYPES:
        result["error_type"] = error["type"]
    if error.get("param") in PARAMETERS:
        result["parameter"] = error["param"]
    message = error.get("message")
    if isinstance(message, str):
        lowered = message.lower()
        result["reported_terms"] = [term for term in TERMS if term in lowered]
    return result
