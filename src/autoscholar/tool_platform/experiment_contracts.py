"""Core-only experiment contracts; never exposed as unrestricted model tools."""

from typing import Any

from autoscholar.tool_platform.gateway import ToolContract

SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
ID = {"type": "string", "format": "uuid", "maxLength": 36}
EXECUTE_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"enum": ["static_check", "run_python", "run_pytest", "run_shell"]},
        "path": {"type": ["string", "null"], "maxLength": 500},
        "args": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 30},
        "collect_artifacts": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "maxItems": 20,
        },
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
        "source_sha256": SHA,
        "dataset_sha256": {"type": ["string", "null"], "maxLength": 64},
    },
    "required": [
        "action",
        "path",
        "args",
        "collect_artifacts",
        "timeout_seconds",
        "source_sha256",
        "dataset_sha256",
    ],
    "additionalProperties": False,
}
CONTROL_INPUT = {
    "type": "object",
    "properties": {"operation_id": ID},
    "required": ["operation_id"],
    "additionalProperties": False,
}
MANIFEST: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"enum": ["succeeded", "failed", "timed_out"]},
        "exit_code": {"type": ["integer", "null"]},
        "stdout": {"type": "string", "maxLength": 65536},
        "stderr": {"type": "string", "maxLength": 65536},
        "duration_ms": {"type": "number", "minimum": 0},
        "truncated": {"type": "boolean"},
        "artifacts": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "id": ID,
                    "path": {"type": "string", "maxLength": 500},
                    "size_bytes": {"type": "integer", "minimum": 0, "maximum": 16777216},
                    "sha256": SHA,
                },
                "required": ["id", "path", "size_bytes", "sha256"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "status",
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
        "truncated",
        "artifacts",
    ],
    "additionalProperties": False,
}
REPLY: dict[str, Any] = {
    "type": "object",
    "properties": {
        "metrics": {"type": ["object", "null"], "maxProperties": 20},
        "status": {
            "enum": [
                "queued",
                "running",
                "completed",
                "cancelled",
                "denied",
                "uncertain",
                "missing",
            ]
        },
        "result": {"anyOf": [MANIFEST, {"type": "null"}]},
        "error_code": {"type": ["string", "null"], "maxLength": 100},
        "uncertain": {"type": "boolean"},
    },
    "required": ["status", "result", "error_code", "uncertain"],
    "additionalProperties": False,
    "allOf": [
        {
            "if": {"properties": {"status": {"const": "completed"}}},
            "then": {"properties": {"result": MANIFEST}},
        }
    ],
}
HEALTH: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"enum": ["ok", "error"]},
        "engine": {"type": "boolean"},
        "image": {"type": "boolean"},
        "mnist_dataset": {"type": "boolean"},
        "dataset_id": {"type": ["string", "null"]},
        "dataset_sha256": {"type": ["string", "null"]},
    },
    "required": ["status", "engine", "image", "mnist_dataset", "dataset_id", "dataset_sha256"],
    "additionalProperties": False,
}
EXPERIMENT_CONTRACTS = [
    ToolContract("execute", EXECUTE_INPUT, REPLY),
    *[
        ToolContract(name, CONTROL_INPUT, REPLY)
        for name in ("get_status", "get_logs", "get_metrics", "cancel")
    ],
    ToolContract(
        "sandbox_health",
        {"type": "object", "properties": {}, "additionalProperties": False},
        HEALTH,
    ),
]
