"""No shell, hooks, credentials, submodules, arbitrary revisions or developer repositories.

HTTPS policy follows https://git-scm.com/docs/git-config (curloptResolve,
followRedirects) and https://git-scm.com/docs/git-diff (no-ext-diff/no-textconv).
"""

import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from autoscholar.coding.workspace import WorkspaceManager


class GitPolicyError(ValueError):
    pass


def public_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitPolicyError("git_url_denied") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)
        or not re.fullmatch(r"/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise GitPolicyError("git_url_denied")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise GitPolicyError("git_url_denied")
    path = parsed.path if parsed.path.endswith(".git") else parsed.path + ".git"
    return "https://" + parsed.hostname.lower() + path


class GitManager:
    def __init__(
        self,
        root: Path,
        workspace: WorkspaceManager,
        *,
        allowed_urls: list[str],
        fetcher: Callable[[str, Path, Callable[[], Awaitable[None]]], Awaitable[None]]
        | None = None,
    ) -> None:
        self.root, self.workspace = root.resolve(), workspace
        if self.root.is_relative_to(
            workspace.root.resolve()
        ) or workspace.root.resolve().is_relative_to(self.root):
            raise ValueError("Git metadata must be separate from task sources")
        self.allowed_urls = {public_url(url) for url in allowed_urls}
        self.fetcher = fetcher or self.fetch_https

    def task_root(self, task_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
            raise GitPolicyError("git_scope_invalid")
        path = self.root / task_id
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(self.root):
            raise GitPolicyError("git_scope_invalid")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def repository(self, task_id: str, repo_id: str) -> Path:
        if not re.fullmatch(r"repo-[0-9a-f]{16}", repo_id):
            raise GitPolicyError("git_repo_invalid")
        path = self.task_root(task_id) / repo_id
        if not path.is_dir() or path.is_symlink() or path.is_junction():
            raise GitPolicyError("git_repo_missing")
        return path

    @staticmethod
    def environment() -> dict[str, str]:
        # No HOME reassignment, inherited Git configuration, proxy, credentials or askpass.
        env = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            LC_ALL="C",
        )
        return env

    @staticmethod
    def options() -> list[str]:
        return [
            item
            for key, value in {
                "protocol.allow": "never",
                "protocol.https.allow": "always",
                "credential.helper": "",
                "http.proxy": "",
                "http.followRedirects": "false",
                "http.sslVerify": "true",
                "http.maxRetries": "0",
                "core.hooksPath": os.devnull,
                "core.fsmonitor": "false",
                "core.autocrlf": "false",
                "submodule.recurse": "false",
                "fetch.fsckObjects": "true",
                "transfer.fsckObjects": "true",
            }.items()
            for item in ("-c", key + "=" + value)
        ]

    async def run(
        self,
        args: list[str],
        directory: Path,
        *,
        limit: int = 65536,
        timeout: float = 3,
        guard: Callable[[], Awaitable[None]] | None = None,
    ) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "git",
            *self.options(),
            *args,
            cwd=directory,
            env=self.environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )

        async def read(stream: asyncio.StreamReader | None) -> bytes:
            assert stream is not None
            content = bytearray()
            while chunk := await stream.read(4096):
                content.extend(chunk)
                if len(content) > limit:
                    raise GitPolicyError("git_output_limit")
            return bytes(content)

        async def monitor() -> None:
            while process.returncode is None:
                if guard is not None:
                    await guard()
                size, count = 0, 0
                for path in directory.rglob("*"):
                    if path.is_symlink() or path.is_junction():
                        raise GitPolicyError("git_link_denied")
                    if path.is_file():
                        size += path.stat().st_size
                        count += 1
                    if size > 64 * 1024**2 or count > 2000:
                        raise GitPolicyError("git_repository_limit")
                await asyncio.sleep(0.2)

        readers = [
            asyncio.create_task(read(process.stdout)),
            asyncio.create_task(read(process.stderr)),
        ]
        watcher = asyncio.create_task(monitor())
        waiter = asyncio.create_task(process.wait())
        try:
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait(
                    {watcher, waiter, *readers}, return_when=asyncio.FIRST_EXCEPTION
                )
                for task in done:
                    task.result()
                if process.returncode != 0:
                    raise GitPolicyError("git_command_failed")
                return readers[0].result()
        finally:
            if process.returncode is None:
                if os.name == "posix":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, 9)  # type: ignore[attr-defined]
                else:
                    process.kill()
            await process.wait()
            for task in (watcher, waiter, *readers):
                if not task.done():
                    task.cancel()
            await asyncio.gather(watcher, waiter, *readers, return_exceptions=True)

    async def fetch_https(
        self, url: str, stage: Path, guard: Callable[[], Awaitable[None]]
    ) -> None:
        host = urlsplit(url).hostname
        assert host is not None
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = sorted({str(item[4][0]) for item in addresses})
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise GitPolicyError("git_network_denied")
        pinned = ",".join("[" + ip + "]" if ":" in ip else ip for ip in ips)
        empty = stage / "empty-template"
        empty.mkdir()
        await self.run(
            [
                "-c",
                f"http.curloptResolve={host}:443:{pinned}",
                "clone",
                "--depth=1",
                "--single-branch",
                "--no-tags",
                "--no-checkout",
                "--no-hardlinks",
                "--template=" + str(empty),
                "--",
                url,
                str(stage / "repository"),
            ],
            stage,
            timeout=90,
            guard=guard,
        )

    async def prepare(
        self, task_id: str, value: str, guard: Callable[[], Awaitable[None]]
    ) -> tuple[Path, str, str, dict[str, str]]:
        url = public_url(value)
        if url not in self.allowed_urls:
            raise GitPolicyError("git_url_not_allowlisted")
        repo_id = "repo-" + hashlib.sha256(url.encode()).hexdigest()[:16]
        task_root = self.task_root(task_id)
        if (task_root / repo_id).exists():
            raise GitPolicyError("git_repo_exists")
        stage = Path(tempfile.mkdtemp(prefix=".autoscholar-clone-", dir=task_root))
        try:
            await self.fetcher(url, stage, guard)
            repository = stage / "repository"
            tree = await self.run(["ls-tree", "-rlz", "HEAD"], repository, guard=guard)
            entries = [entry for entry in tree.split(b"\x00") if entry]
            if not entries or len(entries) > self.workspace.max_files:
                raise GitPolicyError("git_file_limit")
            snapshot: dict[str, str] = {}
            total = 0
            for entry in entries:
                metadata, raw_path = entry.split(b"\t", 1)
                mode, kind, oid, raw_size = metadata.split()
                path = raw_path.decode("utf-8")
                if (
                    mode not in {b"100644", b"100755"}
                    or kind != b"blob"
                    or any(part.lower() == ".git" for part in path.split("/"))
                ):
                    raise GitPolicyError("git_tree_denied")
                size = int(raw_size)
                total += size
                if size > self.workspace.max_file_bytes or total > self.workspace.max_source_bytes:
                    raise GitPolicyError("git_source_limit")
                blob = await self.run(
                    ["cat-file", "blob", oid.decode("ascii")],
                    repository,
                    limit=self.workspace.max_file_bytes,
                    guard=guard,
                )
                try:
                    snapshot[path] = blob.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise GitPolicyError("git_non_text_repository") from exc
                # Validate paths before touching the task source directory.
                self.workspace._resolve_file(task_id, repo_id + "/" + path)
            head = (
                (await self.run(["rev-parse", "HEAD"], repository, guard=guard))
                .decode("ascii")
                .strip()
            )
            await self.run(["read-tree", "HEAD"], repository, guard=guard)
            return stage, repo_id, head, snapshot
        except BaseException:
            self.cleanup(stage, task_id)
            raise

    def cleanup(self, stage: Path, task_id: str) -> None:
        expected = self.task_root(task_id)
        if (
            stage.parent != expected
            or not stage.name.startswith(".autoscholar-clone-")
            or stage.is_symlink()
            or not stage.resolve().is_relative_to(expected)
        ):
            raise GitPolicyError("git_cleanup_scope_invalid")
        if stage.exists():
            shutil.rmtree(stage)

    def publish(self, task_id: str, stage: Path, repo_id: str, snapshot: dict[str, str]) -> None:
        existing = self.workspace.list_source_files(task_id)
        if (
            len(existing) + len(snapshot) > self.workspace.max_files
            or sum(f.size_bytes for f in existing) + sum(len(t.encode()) for t in snapshot.values())
            > self.workspace.max_source_bytes
        ):
            raise GitPolicyError("git_workspace_quota")
        target = self.task_root(task_id) / repo_id
        if target.exists():
            raise GitPolicyError("git_repo_exists")
        for path in snapshot:
            if self.workspace._resolve_file(task_id, repo_id + "/" + path).exists():
                raise GitPolicyError("git_source_exists")
        (stage / "repository").rename(target)
        for path, text in snapshot.items():
            self.workspace.write_text(task_id, repo_id + "/" + path, text)

    async def inspect(self, task_id: str, repo_id: str, *, diff: bool) -> str:
        repo = self.repository(task_id, repo_id)
        source = self.workspace._resolve_file(task_id, repo_id + "/.git-scope-check").parent
        # Checking existing source paths also rejects symlink substitutions before Git sees them.
        self.workspace.list_source_files(task_id)
        args = ["--git-dir=" + str(repo / ".git"), "--work-tree=" + str(source)]
        args += (
            ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "HEAD", "--"]
            if diff
            else ["status", "--porcelain=v1", "--untracked-files=all"]
        )
        return (await self.run(args, repo, timeout=2)).decode("utf-8", errors="replace")
