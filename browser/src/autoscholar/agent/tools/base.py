from dataclasses import dataclass
from typing import Any, Protocol

from autoscholar.llm import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output: str
    succeeded: bool
    duration_ms: float
    error_code: str | None = None


class AgentTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult: ...
