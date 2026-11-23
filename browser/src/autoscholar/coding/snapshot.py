"""Small, atomic authoring operations for already populated source snapshots."""

import copy
import json
from dataclasses import replace

from autoscholar.llm.models import ToolCall, ToolDefinition

MAX_ARGUMENT_BYTES = 4096
TEXT_LIMITS = {
    "create_file": {"path": 500, "content": 2048},
    "edit_file": {"path": 500, "old_text": 1024, "new_text": 1024},
    "submit_code_ready": {"summary": 240},
}


def snapshot_tool(tool: ToolDefinition) -> ToolDefinition:
    parameters = copy.deepcopy(tool.parameters)
    for name, maximum in TEXT_LIMITS[tool.name].items():
        prop = parameters["properties"][name]
        prop["maxLength"] = min(prop.get("maxLength", maximum), maximum)
        if name in {"path", "old_text"}:
            prop["minLength"] = 1
    return replace(
        tool,
        parameters=parameters,
        description=tool.description
        + (
            " Snapshot mode: one short native call only; arguments at most 4096 UTF-8 bytes. "
            "Use the shortest unique old_text and a small local replacement; never echo files."
        ),
    )


def snapshot_violation(call: ToolCall) -> str | None:
    limits = TEXT_LIMITS.get(call.name)
    if limits is None or set(call.arguments) != set(limits):
        return "Use exactly the declared fields of one available native tool."
    for field, maximum in limits.items():
        value = call.arguments[field]
        if not isinstance(value, str) or len(value) > maximum:
            return f"{field} must be text of at most {maximum} characters."
        if field in {"path", "old_text"} and not value:
            return f"{field} must not be empty."
    try:
        size = len(json.dumps(call.arguments, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return "Arguments must be valid UTF-8 JSON text."
    if size > MAX_ARGUMENT_BYTES:
        return f"Combined arguments exceed {MAX_ARGUMENT_BYTES} UTF-8 bytes."
    return None
