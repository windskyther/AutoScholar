"""Filesystem MCP: narrow source operations, durable receipts and workflow fencing."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from autoscholar.coding.tools import WorkspaceToolset
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.infrastructure import Database
from autoscholar.tool_platform.filesystem import file_contracts
from autoscholar.tool_platform.gateway import canonical, validate
from autoscholar.tool_platform.operations import OperationStore, failure
from autoscholar.tool_platform.research_server import ServiceAuthentication, invocation_context


class FilesystemServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    mcp_service_token: SecretStr
    database_url: str
    workspace_root: Path = Path("/data/workspaces")
    workspace_max_files: int = Field(default=100, ge=1, le=1000)
    workspace_max_file_bytes: int = Field(default=1048576, ge=1, le=10485760)
    workspace_max_source_bytes: int = Field(default=10485760, ge=1, le=104857600)
    mcp_allowed_hosts: list[str] = ["filesystem-mcp:8092", "localhost:*", "127.0.0.1:*"]


def create_filesystem_app(
    manager: WorkspaceManager, operations: OperationStore, *, token: str, allowed_hosts: list[str]
) -> Starlette:
    mcp = MCPServer("AutoScholar Filesystem", log_level="WARNING")
    contracts = {c.name: c for c in file_contracts(manager)}

    async def execute(
        name: str, arguments: dict[str, Any], ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        validate(contracts[name].input_schema, arguments, "tool_arguments_invalid")
        context, remaining = invocation_context(name, arguments, ctx)

        async def action() -> dict[str, Any]:
            task_id = context["scope"]["task_id"]
            # Core owns workspace creation/seeding. This service cannot create arbitrary tasks.
            tools = {t.definition.name: t for t in WorkspaceToolset(manager, task_id).tools()}
            result = await tools[name].execute(arguments)
            reply = {
                "succeeded": result.succeeded,
                "output": result.output,
                "error_code": result.error_code,
                "uncertain": False,
            }
            if len(result.output) > 262144 or len(canonical(reply).encode()) > 750000:
                return failure("tool_output_too_large")
            return reply

        async with asyncio.timeout(remaining):
            return await operations.execute(context, action)

    @mcp.tool()
    async def get_operation(operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read a task-bound receipt using the current worker's authority; never replay it."""
        context, remaining = invocation_context(
            "get_operation", {"operation_id": operation_id}, ctx
        )
        async with asyncio.timeout(remaining):
            return await operations.lookup(operation_id, context)

    @mcp.tool()
    async def list_files(ctx: Context[Any, Any]) -> dict[str, Any]:
        """List source files only in the authenticated task workspace."""
        return await execute("list_files", {}, ctx)

    @mcp.tool()
    async def read_file(path: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read a bounded UTF-8 source file in the authenticated task workspace."""
        return await execute("read_file", {"path": path}, ctx)

    @mcp.tool()
    async def search_code(query: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Find literal text in task sources, never execute search expressions."""
        return await execute("search_code", {"query": query}, ctx)

    @mcp.tool()
    async def create_file(path: str, content: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Create a new task source file, with durable deduplication and quota enforcement."""
        return await execute("create_file", {"path": path, "content": content}, ctx)

    @mcp.tool()
    async def edit_file(
        path: str, old_text: str, new_text: str, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Replace one exact match in a task source file; no whole-project access."""
        return await execute(
            "edit_file", {"path": path, "old_text": old_text, "new_text": new_text}, ctx
        )

    @mcp.tool()
    async def delete_file(path: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Delete one source file in the authenticated task workspace."""
        return await execute("delete_file", {"path": path}, ctx)

    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=2 * 1048576,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts, allowed_origins=[]
        ),
    )

    async def live(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "filesystem-mcp"})

    app.routes.append(Route("/health/live", live))
    app.add_middleware(ServiceAuthentication, token=token)
    return app


def create_app() -> Starlette:
    settings = FilesystemServerSettings()  # type: ignore[call-arg]
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("Filesystem MCP requires PostgreSQL row-level locking")
    database = Database(settings.database_url)
    manager = WorkspaceManager(
        settings.workspace_root,
        max_files=settings.workspace_max_files,
        max_file_bytes=settings.workspace_max_file_bytes,
        max_source_bytes=settings.workspace_max_source_bytes,
    )
    app = create_filesystem_app(
        manager,
        OperationStore(database.session_factory, "filesystem"),
        token=settings.mcp_service_token.get_secret_value(),
        allowed_hosts=settings.mcp_allowed_hosts,
    )
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[Any]:
        try:
            async with original(application) as state:
                yield state
        finally:
            await database.close()

    app.router.lifespan_context = lifespan
    return app
