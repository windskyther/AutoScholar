"""Core adapter for the six task-scoped workspace tools."""

import copy
import time
from typing import Any

from autoscholar.agent.tools.base import ToolExecutionResult
from autoscholar.coding.tools import WorkspaceToolset
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.llm import ToolDefinition
from autoscholar.tool_platform.context import ToolScope, tool_scope
from autoscholar.tool_platform.gateway import ToolContract, ToolGateway, ToolGatewayError

FILE_OUTPUT = {
    "type": "object",
    "properties": {
        "succeeded": {"type": "boolean"},
        "output": {"type": "string", "maxLength": 262144},
        "error_code": {"type": ["string", "null"]},
        "uncertain": {"type": "boolean"},
    },
    "required": ["succeeded", "output", "error_code", "uncertain"],
    "additionalProperties": False,
}


def file_contracts(manager: WorkspaceManager) -> list[ToolContract]:
    contracts = []
    for tool in WorkspaceToolset(manager, "contract-only").tools():
        schema = copy.deepcopy(tool.definition.parameters)
        # Leave room for JSON framing, escaped text and the invocation context.
        for key in ("content", "old_text", "new_text"):
            if key in schema["properties"]:
                schema["properties"][key]["maxLength"] = min(manager.max_file_bytes, 65536)
        contracts.append(ToolContract(tool.definition.name, schema, FILE_OUTPUT))
    return contracts


class MCPFileTool:
    def __init__(self, definition: ToolDefinition, gateway: ToolGateway, task_id: str) -> None:
        self._definition, self.gateway, self.task_id = definition, gateway, task_id

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        started = time.perf_counter()
        try:
            with tool_scope(ToolScope(self.task_id)):
                reply = await self.gateway.invoke(self.definition.name, arguments)
            return ToolExecutionResult(
                output=reply["output"],
                succeeded=reply["succeeded"],
                error_code=reply["error_code"],
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except ToolGatewayError as exc:
            if exc.uncertain:
                # Do not let the coding model retry an operation with an unknown outcome.
                raise
            return ToolExecutionResult(
                output=exc.code,
                succeeded=False,
                error_code=exc.code,
                duration_ms=(time.perf_counter() - started) * 1000,
            )


def mcp_file_tools(
    manager: WorkspaceManager, gateway: ToolGateway, task_id: str
) -> list[MCPFileTool]:
    descriptions = {
        t.definition.name: t.definition.description
        for t in WorkspaceToolset(manager, task_id).tools()
    }
    return [
        MCPFileTool(
            ToolDefinition(
                name=c.name, description=descriptions[c.name], parameters=c.input_schema
            ),
            gateway,
            task_id,
        )
        for c in file_contracts(manager)
    ]
