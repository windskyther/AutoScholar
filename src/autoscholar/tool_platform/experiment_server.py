"""Authenticated experiment MCP process: no Docker socket, model or search keys."""

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

from autoscholar.coding.sandbox import SandboxClient
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.infrastructure import Database
from autoscholar.tool_platform.artifact_spool import ArtifactSpool
from autoscholar.tool_platform.experiment_contracts import EXECUTE_INPUT
from autoscholar.tool_platform.experiment_service import ExperimentExecutionService
from autoscholar.tool_platform.gateway import validate
from autoscholar.tool_platform.operations import OperationStore
from autoscholar.tool_platform.research_server import ServiceAuthentication, invocation_context


class ExperimentServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    database_url: str
    mcp_service_token: SecretStr
    workspace_root: Path = Path("/data/workspaces")
    mcp_artifact_root: Path = Path("/data/mcp-artifacts")
    sandbox_manager_url: str = "http://sandbox-manager:8090"
    workflow_approval_threshold: int = Field(default=20000, ge=0)
    mcp_allowed_hosts: list[str] = ["experiment-mcp:8094", "localhost:*", "127.0.0.1:*"]


def create_experiment_app(
    service: ExperimentExecutionService, *, token: str, allowed_hosts: list[str]
) -> Starlette:
    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            await service.close()

    mcp = MCPServer("AutoScholar Experiment", lifespan=lifespan, log_level="WARNING")

    @mcp.tool()
    async def execute(
        action: str,
        path: str | None,
        args: list[str],
        collect_artifacts: list[str],
        timeout_seconds: int,
        source_sha256: str,
        dataset_sha256: str | None,
        ctx: Context[Any, Any],
    ) -> dict[str, Any]:
        """Submit an authorized, source-bound execution once. Never retry uncertain work."""
        arguments = {
            "action": action,
            "path": path,
            "args": args,
            "collect_artifacts": collect_artifacts,
            "timeout_seconds": timeout_seconds,
            "source_sha256": source_sha256,
            "dataset_sha256": dataset_sha256,
        }
        validate(EXECUTE_INPUT, arguments, "tool_arguments_invalid")
        context, remaining = invocation_context("execute", arguments, ctx)
        import asyncio

        async with asyncio.timeout(remaining):
            return await service.submit(context, arguments)

    async def control(name: str, operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        from uuid import UUID

        operation_id = str(UUID(operation_id))
        context, _ = invocation_context(name, {"operation_id": operation_id}, ctx)
        return await service.control(operation_id, context, cancel=name == "cancel")

    @mcp.tool()
    async def get_status(operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read scoped status and the verified result manifest when complete."""
        return await control("get_status", operation_id, ctx)

    @mcp.tool()
    async def get_logs(operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read bounded stdout/stderr from a completed execution; no invented live logs."""
        return await control("get_logs", operation_id, ctx)

    @mcp.tool()
    async def get_metrics(operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read integrity-checked raw metrics; analysis decisions remain in Core."""
        result = await control("get_metrics", operation_id, ctx)
        result = dict(result)
        result["metrics"] = (
            service.spool.metrics(operation_id, result["result"])
            if result["status"] == "completed"
            else None
        )
        return result

    @mcp.tool()
    async def cancel(operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Cancel only this task's execution, without accepting Docker identifiers."""
        return await control("cancel", operation_id, ctx)

    @mcp.tool()
    async def sandbox_health(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Return narrow sandbox/dataset health; no execution or model access."""
        invocation_context("sandbox_health", {}, ctx)
        return (await service.sandbox.health()).model_dump()

    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=65536,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts, allowed_origins=[]
        ),
    )

    async def live(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "experiment-mcp"})

    app.routes.append(Route("/health/live", live))
    app.add_middleware(ServiceAuthentication, token=token)
    return app


def create_app() -> Starlette:
    settings = ExperimentServerSettings()  # type: ignore[call-arg]
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("Experiment MCP requires PostgreSQL fencing")
    database = Database(settings.database_url)
    app = create_experiment_app(
        ExperimentExecutionService(
            OperationStore(database.session_factory, "experiment"),
            WorkspaceManager(settings.workspace_root),
            SandboxClient(settings.sandbox_manager_url, timeout_seconds=620),
            ArtifactSpool(settings.mcp_artifact_root),
            approval_threshold=settings.workflow_approval_threshold,
        ),
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
