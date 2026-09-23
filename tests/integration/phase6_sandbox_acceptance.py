"""Run inside sandbox-manager; real Docker checks, no external API or secrets."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx

from autoscholar.coding.sandbox import DockerSandboxExecutor, SandboxRunRequest
from autoscholar.coding.workspace import WorkspaceError, WorkspaceManager


async def main() -> None:
    executor = DockerSandboxExecutor(
        image=os.environ["SANDBOX_IMAGE"],
        dataset_volume=os.environ["SANDBOX_DATASET_VOLUME"],
        dataset_ready_file=os.environ["SANDBOX_DATASET_READY_FILE"],
    )
    created_containers: set[str] = set()
    created_volumes: set[str] = set()
    waiting = asyncio.Event()

    async def capture_response(response: httpx.Response) -> None:
        if (
            response.request.method == "POST"
            and response.status_code < 300
            and response.request.url.path in {"/containers/create", "/volumes/create"}
        ):
            await response.aread()
            payload = response.json()
            if response.request.url.path == "/containers/create":
                created_containers.add(payload["Id"])
            else:
                created_volumes.add(payload["Name"])

    async def capture_request(request: httpx.Request) -> None:
        if request.url.path.endswith("/wait"):
            waiting.set()

    executor._client.event_hooks = {
        "request": [capture_request],
        "response": [capture_response],
    }
    prefix = "phase6-check-" + uuid4().hex[:10]
    try:
        isolated = await executor.run(
            SandboxRunRequest(
                task_id=prefix + "-isolation",
                action="run_python",
                path="check.py",
                timeout_seconds=10,
                files={
                    "check.py": (
                        "import os, socket\nfrom pathlib import Path\n"
                        "assert os.getuid() != 0\n"
                        "assert not Path('/var/run/docker.sock').exists()\n"
                        "assert not Path('/app/.env').exists()\n"
                        "assert not any(os.getenv(k) for k in "
                        "('LLM_API_KEY', 'TAVILY_API_KEY', 'EXPERIMENT_API_TOKEN'))\n"
                        "try:\n    socket.create_connection(('192.0.2.1', 443), timeout=1)\n"
                        "except OSError:\n    pass\n"
                        "else:\n    raise AssertionError('network unexpectedly available')\n"
                        "print('isolation passed')\n"
                    )
                },
            )
        )
        assert isolated.status == "succeeded", isolated.stderr
        timed_out = await executor.run(
            SandboxRunRequest(
                task_id=prefix + "-timeout",
                action="run_python",
                path="wait.py",
                timeout_seconds=1,
                files={"wait.py": "import time\ntime.sleep(30)\n"},
            )
        )
        assert timed_out.status == "timed_out"
        waiting.clear()
        pending = asyncio.create_task(
            executor.run(
                SandboxRunRequest(
                    task_id=prefix + "-cancel",
                    action="run_python",
                    path="wait.py",
                    timeout_seconds=60,
                    files={"wait.py": "import time\ntime.sleep(60)\n"},
                )
            )
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=20)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancellation did not propagate")
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        # Only inspect IDs observed from this executor, not other users' resources.
        assert len(created_containers) == 6 and len(created_volumes) == 3
        for container in created_containers:
            assert (await executor._client.get(f"/containers/{container}/json")).status_code == 404
        for volume in created_volumes:
            assert (await executor._client.get(f"/volumes/{volume}")).status_code == 404
        with tempfile.TemporaryDirectory(prefix="phase6-symlink-", dir="/tmp") as directory:
            root = Path(directory).resolve()
            assert root.parent == Path("/tmp") and root.name.startswith("phase6-symlink-")
            workspace = WorkspaceManager(root / "workspaces")
            task_root = workspace.initialize("symlink-test")
            outside = root / "outside"
            outside.mkdir()
            (task_root / "source" / "link").symlink_to(outside, target_is_directory=True)
            try:
                workspace.write_text("symlink-test", "link/escape.py", "not permitted")
            except WorkspaceError as exc:
                assert exc.code == "workspace_path_invalid"
            else:
                raise AssertionError("Symlink escape accepted")
            assert not list(outside.iterdir())
        print(
            json.dumps(
                {
                    "acceptance": "passed",
                    "isolation": "passed",
                    "timeout": "passed",
                    "cancellation": "passed",
                    "symlink_escape": "blocked",
                    "cleaned_containers": len(created_containers),
                    "cleaned_volumes": len(created_volumes),
                }
            )
        )
    finally:
        await executor.close()


if __name__ == "__main__":
    asyncio.run(main())
