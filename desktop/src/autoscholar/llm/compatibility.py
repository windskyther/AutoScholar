"""Explicit provider wire compatibility; original tool schemas remain authoritative."""

import copy
from typing import Any, Literal

import httpx

type CompatibilityProfile = Literal["auto", "generic", "deepseek"]

_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_LISTS = {"anyOf", "allOf", "oneOf", "prefixItems"}
_SCHEMA_VALUES = {
    "items",
    "additionalProperties",
    "additionalItems",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
}
_UNSUPPORTED_BOUNDS = {"minLength", "maxLength", "minItems", "maxItems"}


def resolve_profile(profile: CompatibilityProfile, base_url: str) -> CompatibilityProfile:
    if profile != "auto":
        return profile
    url = httpx.URL(base_url)
    return "deepseek" if url.scheme == "https" and url.host == "api.deepseek.com" else "generic"


def deepseek_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project only schema nodes; property names, enum values and examples are data."""
    result = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key in _UNSUPPORTED_BOUNDS:
            node.pop(key, None)
        for key, value in node.items():
            if key in _SCHEMA_MAPS and isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif key in _SCHEMA_LISTS and isinstance(value, list):
                for child in value:
                    visit(child)
            elif key in _SCHEMA_VALUES:
                if isinstance(value, list):
                    for child in value:
                        visit(child)
                else:
                    visit(value)

    visit(result)
    return result


def adapt_deepseek_request(request: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(request)
    for tool in result.get("tools", []):
        function = tool["function"]
        if function.get("strict"):
            function["parameters"] = deepseek_schema(function["parameters"])
    if result.get("tools") and result.get("tool_choice") not in ("auto", "none", None):
        result["thinking"] = {"type": "disabled"}
    return result
