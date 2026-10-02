"""Trusted Core scope, kept separate from model-supplied tool arguments."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolScope:
    task_id: str
    project_id: str | None = None
    document_ids: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return asdict(self)


current_tool_scope: ContextVar[ToolScope | None] = ContextVar("tool_scope", default=None)


@contextmanager
def tool_scope(scope: ToolScope) -> Iterator[None]:
    token = current_tool_scope.set(scope)
    try:
        yield
    finally:
        current_tool_scope.reset(token)
