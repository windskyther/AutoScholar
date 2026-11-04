"""Model correction hints contain schema constraints and server rules, never raw output."""

import json
from typing import Any

from pydantic import BaseModel, ValidationError


def schema_feedback(schema: type[BaseModel], error: ValidationError) -> str:
    allowed: set[str] = set()

    def properties(node: Any) -> None:
        if isinstance(node, dict):
            allowed.update(node.get("properties", {}))
            for value in node.values():
                properties(value)
        elif isinstance(node, list):
            for item in node:
                properties(item)

    properties(schema.model_json_schema())
    errors = []
    for item in error.errors(include_input=False)[:5]:
        constraints = {
            key: value
            for key, value in (item.get("ctx") or {}).items()
            if key in {"max_length", "min_length", "ge", "gt", "le", "lt"}
            and type(value) in {int, float}
        }
        location = [
            part if type(part) is int or part in allowed else "[extra]" for part in item["loc"]
        ]
        errors.append({"location": location, "type": item["type"], "constraints": constraints})
    return "schema errors: " + json.dumps(errors)


def revision_feedback(message: str) -> str:
    if "code_repairs" in message:
        return (
            "code_repair_budget_exhausted: repair_constraints.code_repairs_remaining is zero; "
            "preserve plan.steps for validated coding and its source. Do not include coding "
            "or its upstream steps in rerun_steps. Only an authorized experiment recovery "
            "is possible; an infeasible repair must stop within the existing attempt limit"
        )
    for text in (
        "Revision must retain every original step ID/type and the exact original goal",
        "Revision rerun_steps must be unique",
        "Revision must rerun all unresolved issue steps or their prerequisites",
        "Plan must retain an experiment step",
        "Experiment specification must match the requested specification",
        "Knowledge steps require a project_id",
    ):
        if message == text:
            return text
    return (
        "revision_semantics_invalid: obey repair_constraints, retain the original goal, "
        "step IDs/types, specifications and validated sources; rerun every unresolved issue "
        "and its downstream dependencies without increasing any budget or approval"
    )
