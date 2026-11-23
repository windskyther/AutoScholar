"""Task-local stage labels for usage accounting; never sent as model context."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

current_llm_stage: ContextVar[str] = ContextVar("llm_stage", default="other")
STAGES = frozenset({"planning", "screening", "reading", "refining", "writing", "review", "other"})


@contextmanager
def llm_stage(stage: str) -> Iterator[None]:
    token = current_llm_stage.set(stage if stage in STAGES else "other")
    try:
        yield
    finally:
        current_llm_stage.reset(token)


def stage_for_tools(names: list[str]) -> str:
    explicit = current_llm_stage.get()
    if explicit != "other":
        return explicit
    first = names[0] if names else ""
    if first in {
        "submit_task_plan",
        "submit_plan",
        "submit_research_queries",
        "submit_plan_revision",
    }:
        return "planning"
    return {
        "submit_evidence": "screening",
        "submit_research_report": "writing",
        "submit_review": "review",
    }.get(first, "other")
