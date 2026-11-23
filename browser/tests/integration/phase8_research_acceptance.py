"""Credential-free Docker acceptance. Run with the project Python after building phase8-test.

Creates an internal network and private PostgreSQL/Research MCP/client containers.
No production volumes or .env are mounted; only this script is mounted read-only.
"""

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


async def client(phase: str) -> None:
    from sqlalchemy import select

    from autoscholar.agent.database_models import AgentTaskRow, Base
    from autoscholar.core.journal import current_journal
    from autoscholar.infrastructure import Database
    from autoscholar.rag.database_models import DocumentChunkRow, DocumentRow, ProjectRow
    from autoscholar.research import SearchProviderError
    from autoscholar.tool_platform.context import ToolScope, tool_scope
    from autoscholar.tool_platform.gateway import ToolGateway
    from autoscholar.tool_platform.research import RESEARCH_CONTRACTS, MCPResearchSearch
    from autoscholar.tool_platform.transport import MCPBackend

    gateway = ToolGateway(
        MCPBackend(
            os.environ["ACCEPTANCE_URL"], os.environ["MCP_SERVICE_TOKEN"], timeout_seconds=2
        ),
        RESEARCH_CONTRACTS,
        timeout_seconds=2,
    )
    events: list[bool] = []

    async def journal(operation_id: str, kind: str, starting: bool) -> None:
        events.append(starting)

    token = current_journal.set(journal)
    try:
        if phase == "down":
            assert not await gateway.available()
            try:
                await MCPResearchSearch(gateway, "web").search("public MNIST")
            except SearchProviderError as exc:
                assert exc.code in {"tool_unavailable", "tool_timeout"}
            else:
                raise AssertionError("Stopped service unexpectedly answered")
            assert not events, "Pre-dispatch outage created a pending call"
        else:
            async with asyncio.timeout(30):
                while not await gateway.available():
                    await asyncio.sleep(0.2)
            if phase == "factory":
                try:
                    await MCPResearchSearch(gateway, "web").search("public MNIST")
                except SearchProviderError as exc:
                    assert exc.code == "mcp_search_not_configured"
                else:
                    raise AssertionError("Credential-free production provider unexpectedly ran")
                assert events == [True, False]
            else:
                for source in ("web", "paper"):
                    response = await MCPResearchSearch(gateway, source).search("公开 MNIST")
                    assert response.results[0].content == "Offline public evidence"
                assert events == [True, False, True, False]
            database = Database(os.environ["ACCEPTANCE_DATABASE_URL"])
            try:
                async with database._engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with database.session_factory() as session:
                    if (
                        await session.scalar(
                            select(ProjectRow.id).where(ProjectRow.id == "fixture-project")
                        )
                        is None
                    ):
                        session.add(ProjectRow(id="fixture-project", name="Offline fixture"))
                        await session.flush()
                        session.add(
                            AgentTaskRow(
                                id="fixture-task",
                                project_id="fixture-project",
                                status="running",
                                objective="Read public fixture",
                            )
                        )
                        session.add(
                            DocumentRow(
                                id="fixture-document",
                                project_id="fixture-project",
                                title="Public fixture",
                                original_filename="fixture.pdf",
                                content_type="application/pdf",
                                storage_key="fixture",
                                sha256="0" * 64,
                                size_bytes=1,
                                status="ready",
                                chunk_count=1,
                            )
                        )
                        await session.flush()
                        session.add(
                            DocumentChunkRow(
                                id="fixture-chunk",
                                project_id="fixture-project",
                                document_id="fixture-document",
                                ordinal=0,
                                page=1,
                                content="Offline indexed evidence",
                                token_count=4,
                            )
                        )
                        await session.commit()
                arguments = {"document_id": "fixture-document", "offset": 0, "limit": 1}
                with tool_scope(
                    ToolScope("fixture-task", "fixture-project", ("fixture-document",))
                ):
                    result = await gateway.invoke("get_document", arguments)
                    assert (
                        result["ok"]
                        and result["response"]["chunks"][0]["content"] == "Offline indexed evidence"
                    )
                result = await gateway.invoke("get_document", arguments)
                assert result["error_code"] == "document_access_denied"
            finally:
                await database.close()
        print(
            json.dumps({"phase": phase, "acceptance": "passed", "external_api_calls": 0}),
            flush=True,
        )
    finally:
        current_journal.reset(token)


def serve() -> None:
    import uvicorn

    from autoscholar.infrastructure import Database
    from autoscholar.research import SearchResponse, SearchResult, SourceType
    from autoscholar.tool_platform.documents import DocumentReader
    from autoscholar.tool_platform.research_server import create_research_app

    class OfflineSearch:
        configured = True

        def __init__(self, source: SourceType) -> None:
            self.source_type = source
            self.name = "tavily" if source == "web" else "semantic_scholar"

        async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
            return SearchResponse(
                provider=self.name,
                source_type=self.source_type,
                query=query,
                results=(
                    SearchResult(
                        source_type=self.source_type,
                        provider=self.name,
                        title="公开资料",
                        url="https://example.org/public",
                        content="Offline public evidence",
                    ),
                ),
            )

        async def close(self) -> None:
            pass

    database = Database(os.environ["ACCEPTANCE_DATABASE_URL"])
    app = create_research_app(
        [OfflineSearch("web"), OfflineSearch("paper")],
        token=os.environ["MCP_SERVICE_TOKEN"],
        allowed_hosts=["research:8091"],
        documents=DocumentReader(database.session_factory),
    )
    uvicorn.run(app, host="0.0.0.0", port=8091, log_level="error", access_log=False)


def docker(*arguments: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    if check and result.returncode:
        # Never include the argv or inherited environment in an exception.
        raise RuntimeError(
            "Docker acceptance command failed: " + result.stderr[-2000:] + result.stdout[-2000:]
        )
    return result.stdout.strip()


def orchestrate() -> None:
    tag = "phase8-research-" + uuid4().hex[:10]
    label = "autoscholar.acceptance=" + tag
    containers: list[str] = []
    network_created = False
    env = dict(os.environ, MCP_SERVICE_TOKEN=secrets.token_urlsafe(32))
    database_url = "postgresql+asyncpg://fixture:fixture@postgres:5432/fixture"
    script = Path(__file__).resolve().as_posix()
    image = "autoscholar-api:phase8-test"

    def create(name: str, *args: str) -> str:
        container = docker(
            "create", "--name", tag + name, "--label", label, "--network", tag, *args, env=env
        )
        containers.append(container)
        return container

    common = [
        "--mount",
        f"type=bind,source={script},target=/tmp/acceptance.py,readonly",
        "-e",
        "MCP_SERVICE_TOKEN",
        "-e",
        "ACCEPTANCE_DATABASE_URL=" + database_url,
        "--memory",
        "512m",
        "--pids-limit",
        "128",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16777216",
    ]
    try:
        docker("network", "create", "--internal", "--label", label, tag)
        network_created = True
        postgres = create(
            "-postgres",
            "--network-alias",
            "postgres",
            "-e",
            "POSTGRES_DB=fixture",
            "-e",
            "POSTGRES_USER=fixture",
            "-e",
            "POSTGRES_PASSWORD=fixture",
            "postgres:16-alpine",
        )
        docker("start", postgres)
        deadline = time.monotonic() + 30
        while "accepting connections" not in docker(
            "exec", postgres, "pg_isready", "-U", "fixture", check=False
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("Isolated PostgreSQL startup timeout")
            time.sleep(0.2)
        server = create(
            "-server",
            "--network-alias",
            "research",
            *common,
            image,
            "python",
            "/tmp/acceptance.py",
            "--server",
        )
        docker("start", server)
        for phase in ("healthy", "down", "recovered", "factory"):
            url = "http://research:8091/mcp"
            if phase == "down":
                docker("stop", "--time", "10", server)
            elif phase == "recovered":
                docker("start", server)
            elif phase == "factory":
                production = create(
                    "-factory-server",
                    "--network-alias",
                    "factory",
                    *common,
                    "-e",
                    "MCP_DOCUMENT_DATABASE_URL=" + database_url,
                    "-e",
                    'MCP_ALLOWED_HOSTS=["factory:8091"]',
                    "-e",
                    "RESEARCH_CACHE_TTL_SECONDS=0",
                    image,
                    "uvicorn",
                    "autoscholar.tool_platform.research_server:create_app",
                    "--factory",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8091",
                    "--no-access-log",
                )
                docker("start", production)
                url = "http://factory:8091/mcp"
            actor = create(
                "-" + phase,
                *common,
                "-e",
                "ACCEPTANCE_URL=" + url,
                image,
                "python",
                "/tmp/acceptance.py",
                "--client",
                phase,
            )
            docker("start", actor)
            exit_code = docker("wait", actor)
            logs = docker("logs", actor)
            print(logs, flush=True)
            if exit_code != "0":
                print(docker("logs", server), flush=True)
                raise AssertionError("Acceptance client failed")
    finally:
        for container in reversed(containers):
            state: dict[str, Any] = json.loads(docker("inspect", container))[0]
            assert state["Config"]["Labels"].get("autoscholar.acceptance") == tag
            docker("rm", "--force", "--volumes", container)
        if network_created:
            state = json.loads(docker("network", "inspect", tag))[0]
            assert state["Labels"].get("autoscholar.acceptance") == tag
            docker("network", "rm", tag)
        print(json.dumps({"cleanup": "passed", "isolated_stack": tag}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--client", choices=["healthy", "down", "recovered", "factory"])
    options = parser.parse_args()
    if options.server or options.client:
        assert not os.getenv("LLM_API_KEY") and not os.getenv("TAVILY_API_KEY")
    if options.server:
        serve()
    elif options.client:
        asyncio.run(client(options.client))
    else:
        orchestrate()
