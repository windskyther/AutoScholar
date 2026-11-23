"""Task Git service with authenticated metadata, durable reservations and bounded publication."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager
from autoscholar.infrastructure import Database
from autoscholar.tool_platform.gateway import validate
from autoscholar.tool_platform.git_manager import GitManager, GitPolicyError
from autoscholar.tool_platform.git_tools import GIT_CONTRACTS
from autoscholar.tool_platform.operations import OperationStore, failure
from autoscholar.tool_platform.research_server import ServiceAuthentication, invocation_context


class GitServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    database_url: str
    mcp_service_token: SecretStr
    mcp_git_root: Path = Path("/data/git")
    workspace_root: Path = Path("/data/workspaces")
    mcp_git_allowed_urls: list[str] = []
    mcp_allowed_hosts: list[str] = ["git-mcp:8093", "localhost:*", "127.0.0.1:*"]


def success(value: Any) -> dict[str, Any]:
    return {
        "succeeded": True,
        "output": json.dumps(value, ensure_ascii=False),
        "error_code": None,
        "uncertain": False,
    }


def create_git_app(
    manager: GitManager, operations: OperationStore, *, token: str, allowed_hosts: list[str]
) -> Starlette:
    mcp = MCPServer("AutoScholar Git", log_level="WARNING")
    contracts = {contract.name: contract for contract in GIT_CONTRACTS}

    @mcp.tool()
    async def clone_repo(repo_url: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Import only an allowlisted public HTTPS text repository into task sources."""
        args = {"repo_url": repo_url}
        validate(contracts["clone_repo"].input_schema, args, "tool_arguments_invalid")
        context, remaining = invocation_context("clone_repo", args, ctx)
        prior = await operations.reserve(context)
        if prior is not None:
            return prior
        stage: Path | None = None
        task_id = context["scope"]["task_id"]

        async def guard() -> None:
            async with asyncio.timeout(2), operations.sessions() as session:
                await operations.authorize(session, context)

        try:
            async with asyncio.timeout(remaining):
                stage, repo_id, head, snapshot = await manager.prepare(task_id, repo_url, guard)

                async def publish() -> dict[str, Any]:
                    assert stage is not None
                    manager.publish(task_id, stage, repo_id, snapshot)
                    return success(
                        {
                            "repo_id": repo_id,
                            "commit": head,
                            "source_prefix": repo_id + "/",
                            "files": list(snapshot),
                        }
                    )

                return await operations.finish(context, publish)
        except GitPolicyError as error:
            # Policy/network failure before publication is a known failed clone.
            error_code = str(error)

            async def reject() -> dict[str, Any]:
                return failure(error_code)

            return await operations.finish(context, reject)
        finally:
            if stage is not None:
                manager.cleanup(stage, task_id)

    async def inspect(repo_id: str, ctx: Context[Any, Any], *, diff: bool) -> dict[str, Any]:
        name, args = "git_diff" if diff else "git_status", {"repo_id": repo_id}
        validate(contracts[name].input_schema, args, "tool_arguments_invalid")
        context, remaining = invocation_context(name, args, ctx)

        async def action() -> dict[str, Any]:
            try:
                result = await manager.inspect(context["scope"]["task_id"], repo_id, diff=diff)
                return success({"repo_id": repo_id, "diff" if diff else "status": result})
            except (GitPolicyError, WorkspaceError) as exc:
                return failure(str(exc) if isinstance(exc, GitPolicyError) else exc.code)

        async with asyncio.timeout(remaining):
            return await operations.execute(context, action)

    @mcp.tool()
    async def git_status(repo_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read only the status of the authenticated task's imported repository."""
        return await inspect(repo_id, ctx, diff=False)

    @mcp.tool()
    async def git_diff(repo_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read a bounded diff, with external diff and text conversion disabled."""
        return await inspect(repo_id, ctx, diff=True)

    @mcp.tool()
    async def get_operation(operation_id: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Read the current task's immutable Git operation receipt."""
        context, _ = invocation_context("get_operation", {"operation_id": operation_id}, ctx)
        return await operations.lookup(operation_id, context)

    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=65536,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts, allowed_origins=[]
        ),
    )

    async def live(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "git-mcp"})

    app.routes.append(Route("/health/live", live))
    app.add_middleware(ServiceAuthentication, token=token)
    return app


def create_app() -> Starlette:
    settings = GitServerSettings()  # type: ignore[call-arg]
    if os.name != "posix" or not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("Git MCP production requires Linux process groups and PostgreSQL locking")
    database = Database(settings.database_url)
    app = create_git_app(
        GitManager(
            settings.mcp_git_root,
            WorkspaceManager(settings.workspace_root),
            allowed_urls=settings.mcp_git_allowed_urls,
        ),
        OperationStore(database.session_factory, "git"),
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
