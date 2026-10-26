"""Server-owned stage responsibilities. Planner prose cannot add cross-stage duties."""

from typing import Any

from autoscholar.orchestration.models import TaskPlan

# id -> (owner, verified check). None means source/requirement evidence is still needed.
REQUIREMENTS: dict[str, tuple[str, str | None]] = {
    "coding.source_contract": ("coding", None),
    "coding.static_check": ("coding", "static_check"),
    "coding.pytest": ("coding", "pytest"),
    "coding.source_handoff": ("coding", "source_handoff"),
    "experiment.training": ("experiment", "training_execution"),
    "experiment.raw_metrics": ("experiment", "raw_metrics"),
    "experiment.artifacts": ("experiment", "artifacts"),
    "experiment.source_handoff": ("experiment", "source_handoff"),
    "research.citations": ("research", "verbatim_citation"),
    "knowledge.citations": ("knowledge", "verbatim_citation"),
}

_OUTPUTS = {
    "coding": (
        "Source implementing the fixed config/CLI/output contract; static checks and pytest. "
        "Training, collected metrics/checkpoints and platform reports belong to experiment."
    ),
    "experiment": (
        "Execute the validated source with the fixed specification; collect and validate "
        "measured metrics, checkpoints, plots and platform reports."
    ),
    "research": "Supported evidence and citations from the permitted research sources.",
    "knowledge": "Supported evidence and citations from the selected project documents.",
}


def stage_contract(step_type: str) -> dict[str, Any]:
    return {
        "required_output": _OUTPUTS[step_type],
        "requirement_ids": [key for key, (owner, _) in REQUIREMENTS.items() if owner == step_type],
    }


def stage_contracts() -> dict[str, dict[str, Any]]:
    return {kind: stage_contract(kind) for kind in _OUTPUTS}


def review_plan(plan: TaskPlan) -> dict[str, Any]:
    """Supply authoritative outputs to review without mutating the persisted plan."""
    value = plan.model_dump()
    for step in value["steps"]:
        step["expected_output"] = _OUTPUTS[step["type"]]
    return value
