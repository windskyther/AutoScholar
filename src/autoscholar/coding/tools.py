import json
import time
from collections.abc import Callable
from typing import Any

from autoscholar.agent.tools.base import ToolExecutionResult
from autoscholar.coding.sandbox import SandboxAction, SandboxExecutor, SandboxRunRequest
from autoscholar.coding.workspace import WorkspaceError, WorkspaceFile, WorkspaceManager
from autoscholar.llm import ToolDefinition


class WorkspaceTool:
    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[dict[str, Any]], object],
    ) -> None:
        self._definition = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
        )
        self._handler = handler

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        started = time.perf_counter()
        try:
            output = self._handler(arguments)
            return ToolExecutionResult(
                output=json.dumps(output, ensure_ascii=False),
                succeeded=True,
                duration_ms=round((time.perf_counter() - started) * 1_000, 3),
            )
        except WorkspaceError as exc:
            return ToolExecutionResult(
                output=exc.message,
                succeeded=False,
                duration_ms=round((time.perf_counter() - started) * 1_000, 3),
                error_code=exc.code,
            )


class WorkspaceToolset:
    """Creates file tools permanently scoped to one task's source directory."""

    def __init__(self, manager: WorkspaceManager, task_id: str) -> None:
        self._manager = manager
        self._task_id = task_id

    def tools(self) -> list[WorkspaceTool]:
        path = {"type": "string", "minLength": 1, "maxLength": 500}
        text = {"type": "string", "maxLength": self._manager.max_file_bytes}
        return [
            WorkspaceTool(
                name="list_files",
                description="List UTF-8 source files in the current task workspace.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _: [
                    self._record(item)
                    for item in self._manager.list_source_files(self._task_id)
                ],
            ),
            WorkspaceTool(
                name="read_file",
                description="Read one UTF-8 source file from the current task workspace.",
                parameters=self._schema({"path": path}, ["path"]),
                handler=lambda args: {
                    "path": args["path"],
                    "content": self._manager.read_text(self._task_id, args["path"]),
                },
            ),
            WorkspaceTool(
                name="search_code",
                description="Find literal text in source files in the current task workspace.",
                parameters=self._schema(
                    {"query": {"type": "string", "minLength": 1, "maxLength": 500}},
                    ["query"],
                ),
                handler=lambda args: self._manager.search(self._task_id, args["query"]),
            ),
            WorkspaceTool(
                name="create_file",
                description="Create one new UTF-8 source file.",
                parameters=self._schema({"path": path, "content": text}, ["path", "content"]),
                handler=lambda args: self._record(
                    self._manager.write_text(self._task_id, args["path"], args["content"])
                ),
            ),
            WorkspaceTool(
                name="edit_file",
                description="Atomically replace text that occurs exactly once in a source file.",
                parameters=self._schema(
                    {"path": path, "old_text": text, "new_text": text},
                    ["path", "old_text", "new_text"],
                ),
                handler=lambda args: self._record(
                    self._manager.edit_text(
                        self._task_id, args["path"], args["old_text"], args["new_text"]
                    )
                ),
            ),
            WorkspaceTool(
                name="delete_file",
                description="Delete one source file from the current task workspace.",
                parameters=self._schema({"path": path}, ["path"]),
                handler=self._delete,
            ),
        ]

    def _delete(self, arguments: dict[str, Any]) -> dict[str, str]:
        self._manager.delete_file(self._task_id, arguments["path"])
        return {"path": arguments["path"], "status": "deleted"}

    @staticmethod
    def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _record(item: WorkspaceFile) -> dict[str, object]:
        return {
            "path": item.path,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
        }


class SandboxTool:
    def __init__(
        self,
        *,
        definition: ToolDefinition,
        manager: WorkspaceManager,
        sandbox: SandboxExecutor,
        task_id: str,
        timeout_seconds: int,
        action: SandboxAction,
    ) -> None:
        self._definition = definition
        self._manager = manager
        self._sandbox = sandbox
        self._task_id = task_id
        self._timeout = timeout_seconds
        self._action = action

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        started = time.perf_counter()
        payload = dict(arguments)
        try:
            request = SandboxRunRequest(
                task_id=self._task_id,
                action=self._action,
                path=payload.pop("path", None) or payload.pop("executable", None),
                args=payload.pop("args", []),
                files=self._manager.source_snapshot(self._task_id),
                timeout_seconds=self._timeout,
            )
            result = await self._sandbox.run(request)
            output = json.dumps(result.model_dump(), ensure_ascii=False)
            return ToolExecutionResult(
                output=output,
                succeeded=result.status == "succeeded",
                duration_ms=round((time.perf_counter() - started) * 1_000, 3),
                error_code=None if result.status == "succeeded" else f"sandbox_{result.status}",
            )
        except (WorkspaceError, ValueError) as exc:
            return ToolExecutionResult(
                output=str(exc),
                succeeded=False,
                duration_ms=round((time.perf_counter() - started) * 1_000, 3),
                error_code=getattr(exc, "code", "sandbox_request_invalid"),
            )


class SandboxToolset:
    def __init__(
        self,
        manager: WorkspaceManager,
        sandbox: SandboxExecutor,
        task_id: str,
        timeout_seconds: int,
    ) -> None:
        self._manager = manager
        self._sandbox = sandbox
        self._task_id = task_id
        self._timeout = timeout_seconds

    def tools(self) -> list[SandboxTool]:
        args = {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "maxItems": 30,
        }
        path = {"type": "string", "minLength": 1, "maxLength": 500}
        return [
            self._tool(
                "run_python",
                "Run one Python source file in the isolated, offline sandbox.",
                "run_python",
                {"path": path, "args": args},
                ["path"],
            ),
            self._tool(
                "run_pytest",
                "Run pytest in the isolated, offline sandbox.",
                "run_pytest",
                {"args": args},
                [],
            ),
            self._tool(
                "run_shell",
                "Run an approved executable without shell parsing in the isolated sandbox.",
                "run_shell",
                {
                    "executable": {"type": "string", "enum": ["python", "pytest", "ruff"]},
                    "args": args,
                },
                ["executable"],
            ),
        ]

    def _tool(
        self,
        name: str,
        description: str,
        action: SandboxAction,
        properties: dict[str, Any],
        required: list[str],
    ) -> SandboxTool:
        return SandboxTool(
            definition=ToolDefinition(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            ),
            manager=self._manager,
            sandbox=self._sandbox,
            task_id=self._task_id,
            timeout_seconds=self._timeout,
            action=action,
        )
