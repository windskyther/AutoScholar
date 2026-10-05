import asyncio
import json
import socket
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from autoscholar.tool_platform.context import ToolScope, tool_scope
from autoscholar.tool_platform.gateway import ToolGateway, ToolGatewayError
from autoscholar.tool_platform.git_manager import GitManager, GitPolicyError, public_url
from autoscholar.tool_platform.git_server import create_git_app
from autoscholar.tool_platform.git_tools import GIT_CONTRACTS
from autoscholar.tool_platform.operations import OperationDenied, OperationStore
from autoscholar.tool_platform.transport import MCPBackend

pytest_plugins = ["tests.test_filesystem_mcp"]
URL = "https://example.org/fixture/research.git"
TOKEN = "offline-git-service-token-at-least-32-characters"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture-origin"
    repo.mkdir()
    commands = [["init", str(repo)], ["add", "."], ["commit", "-m", "Public fixture"]]
    for command in commands:
        if command[0] == "add":
            (repo / "model.py").write_text("value = 'one'\n", encoding="utf-8")
            (repo / "README.md").write_text("Public offline fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", *command],
            cwd=repo,
            env=GitManager.environment(),
            capture_output=True,
            check=True,
        )
    return repo


@pytest.fixture
def git_manager(filesystem: Any, origin: Path, tmp_path: Path) -> GitManager:
    _, workspace = filesystem
    holder: list[GitManager] = []

    async def fixture_fetch(url: str, stage: Path, guard: Callable[[], Awaitable[None]]) -> None:
        assert url == URL
        await holder[0].run(
            [
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--no-checkout",
                "--no-hardlinks",
                "--template=",
                "--",
                str(origin),
                str(stage / "repository"),
            ],
            stage,
            timeout=5,
            guard=guard,
        )

    manager = GitManager(
        tmp_path / "protected-git", workspace, allowed_urls=[URL], fetcher=fixture_fetch
    )
    holder.append(manager)
    return manager


@pytest.fixture
async def git_http(git_manager: GitManager, filesystem: Any) -> AsyncIterator[ToolGateway]:
    operations, _ = filesystem
    app = create_git_app(
        git_manager,
        OperationStore(operations.sessions, "git"),
        token=TOKEN,
        allowed_hosts=["127.0.0.1:*"],
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield ToolGateway(
            MCPBackend(f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", TOKEN),
            GIT_CONTRACTS,
            timeout_seconds=10,
        )
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 5)
        finally:
            sock.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "git@github.com:owner/repo",
        "http://example.org/a/b",
        "https://user:pass@example.org/a/b",
        "https://example.org/a/b?token=secret",
        "https://127.0.0.1/a/b",
        "https://example.org:444/a/b",
        "https://example.org/a/../b",
    ],
)
def test_public_url_policy(url: str) -> None:
    with pytest.raises(GitPolicyError):
        public_url(url)


async def test_real_git_clone_edit_status_diff_and_task_scope(
    git_http: ToolGateway, git_manager: GitManager
) -> None:
    assert (await git_http.invoke("clone_repo", {"repo_url": URL}))[
        "error_code"
    ] == "task_scope_required"
    with tool_scope(ToolScope("file-task")):
        assert (
            await git_http.invoke("clone_repo", {"repo_url": "https://example.org/other/repo"})
        )["error_code"] == "git_url_not_allowlisted"
        result = await git_http.invoke("clone_repo", {"repo_url": URL})
        assert result["succeeded"], result
        imported = json.loads(result["output"])
        repo_id = imported["repo_id"]
        assert imported["files"] == ["README.md", "model.py"]
        assert (
            json.loads((await git_http.invoke("git_status", {"repo_id": repo_id}))["output"])[
                "status"
            ]
            == ""
        )
        git_manager.workspace.edit_text("file-task", repo_id + "/model.py", "one", "two")
        diff = json.loads((await git_http.invoke("git_diff", {"repo_id": repo_id}))["output"])[
            "diff"
        ]
        assert "+value = 'two'" in diff
        assert (
            "model.py"
            in json.loads((await git_http.invoke("git_status", {"repo_id": repo_id}))["output"])[
                "status"
            ]
        )
        assert (await git_http.invoke("clone_repo", {"repo_url": URL}))[
            "error_code"
        ] == "git_repo_exists"
        with pytest.raises(ToolGatewayError, match="tool_not_allowed"):
            await git_http.invoke("git_push", {"repo_id": repo_id})
    assert not list(git_manager.task_root("file-task").glob(".autoscholar-clone-*"))
    assert not (git_manager.workspace.root / "file-task" / "source" / repo_id / ".git").exists()


async def test_clone_cancellation_cleans_stage_and_does_not_publish(
    git_manager: GitManager,
) -> None:
    async def guard() -> None:
        raise OperationDenied("workflow_claim_stale")

    with pytest.raises(OperationDenied):
        await git_manager.prepare("file-task", URL, guard)
    assert not git_manager.workspace.list_source_files("file-task")
    assert not list(git_manager.task_root("file-task").glob(".autoscholar-clone-*"))


async def test_binary_tree_rejected_before_source_publication(
    git_manager: GitManager, origin: Path
) -> None:
    (origin / "binary.bin").write_bytes(b"\xff\xfe")
    for args in (["add", "."], ["commit", "-m", "Binary fixture"]):
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", *args],
            cwd=origin,
            env=GitManager.environment(),
            capture_output=True,
            check=True,
        )

    async def guard() -> None:
        return None

    with pytest.raises(GitPolicyError, match="git_non_text_repository"):
        await git_manager.prepare("file-task", URL, guard)
    assert not git_manager.workspace.list_source_files("file-task")
    assert not list(git_manager.task_root("file-task").glob(".autoscholar-clone-*"))
