"""Inside a disposable trusted manager with only source, fixture volume and Docker socket."""

import asyncio
import json
import os

import httpx
from pydantic import SecretStr

from autoscholar.coding.sandbox import (
    DockerSandboxExecutor,
    SandboxClient,
    SandboxError,
    SandboxRunRequest,
)
from autoscholar.sandbox.access import ManagerSettings
from autoscholar.sandbox.manager import create_manager_app


async def main() -> None:
    token = SecretStr("public-sandbox-control-token-0123456789")
    executor = DockerSandboxExecutor(
        image=os.environ["SANDBOX_IMAGE"],
        dataset_volume=os.environ["SANDBOX_DATASET_VOLUME"],
        dataset_ready_file="/datasets/mnist/.unused-public-fixture",
        evaluation_owner=os.environ["PHASE11D_OWNER"],
    )
    created: dict[str, str] = {}
    creating: list[str] = []
    waiting, cleaning = asyncio.Event(), asyncio.Event()

    async def capture(request: httpx.Request) -> None:
        if request.method == "POST" and request.url.path in {
            "/containers/create",
            "/volumes/create",
        }:
            creating.append(request.url.path)
        if request.url.path.endswith("/wait"):
            waiting.set()
        if request.method == "DELETE":
            cleaning.set()

    async def response_hook(response: httpx.Response) -> None:
        if response.status_code >= 400 and response.status_code != 404:
            await response.aread()
            # This isolated fixture has no real credentials or user datasets.
            print(
                json.dumps(
                    {
                        "fixture_engine_status": response.status_code,
                        "fixture_engine_error": response.text[:1000],
                    }
                )
            )
        if (
            response.request.method == "POST"
            and response.status_code < 300
            and response.request.url.path in {"/containers/create", "/volumes/create"}
        ):
            await response.aread()
            payload = response.json()
            created[payload.get("Id") or payload["Name"]] = (
                "container" if "Id" in payload else "volume"
            )

    executor._client.event_hooks = {"request": [capture], "response": [response_hook]}
    app = create_manager_app(executor, settings=ManagerSettings(sandbox_manager_token=token))
    client = SandboxClient(
        "http://manager", token=token, transport=httpx.ASGITransport(app=app), timeout_seconds=60
    )
    payload = SandboxRunRequest(
        task_id="phase11d-isolation",
        action="run_python",
        path="check.py",
        files={
            "check.py": (
                "import os,socket\nfrom pathlib import Path\n"
                "assert os.getuid()==65532\n"
                "assert not Path('/var/run/docker.sock').exists()\n"
                "assert not Path('/app/.env').exists()\n"
                "assert not any('TOKEN' in key or 'API_KEY' in key for key in os.environ)\n"
                "for target in ['/root-write-test','/datasets/mnist/write-test']:\n"
                " try: Path(target).write_text('no')\n"
                " except OSError: pass\n"
                " else: raise AssertionError('writable protected path')\n"
                "for target in [('192.0.2.1',443),('172.17.0.1',2375),('1.1.1.1',53)]:\n"
                " try: socket.create_connection(target,timeout=.2)\n"
                " except OSError: pass\n"
                " else: raise AssertionError('network escape')\n"
                "print('isolation passed')\n"
                "Path('metrics.json').write_text('{\"public\": true}')\n"
            )
        },
        timeout_seconds=10,
        collect_artifacts=["metrics.json"],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://manager"
        ) as unauthenticated:
            denied = await unauthenticated.post("/internal/v1/run", content=b"invalid JSON")
            assert denied.status_code == 401 and not creating
        result = await client.run(payload)
        assert result.status == "succeeded", result.stderr
        assert len(result.artifacts) == 1 and result.artifacts[0].path == "metrics.json"
        timeout = await client.run(
            payload.model_copy(
                update={
                    "files": {"check.py": "import time; time.sleep(30)"},
                    "timeout_seconds": 1,
                    "collect_artifacts": [],
                }
            )
        )
        assert timeout.status == "timed_out"
        waiting.clear()
        cleaning.clear()
        pending = asyncio.create_task(
            executor.run(
                payload.model_copy(
                    update={
                        "files": {"check.py": "import time; time.sleep(60)"},
                        "timeout_seconds": 60,
                        "collect_artifacts": [],
                    }
                )
            )
        )
        await asyncio.wait_for(waiting.wait(), 20)
        # The loader has been removed before the runner waits. Observe runner cleanup.
        cleaning.clear()
        pending.cancel()
        await asyncio.wait_for(cleaning.wait(), 10)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Repeated cancellation was not propagated")
        try:
            await client.run(
                payload.model_copy(
                    update={
                        "files": {
                            "check.py": (
                                "from pathlib import Path; "
                                "Path('metrics.json').symlink_to('/etc/passwd')"
                            )
                        },
                        "collect_artifacts": ["metrics.json"],
                    }
                )
            )
        except SandboxError:
            pass
        else:
            raise AssertionError("Symlink artifact accepted")
        for name, kind in created.items():
            path = f"/containers/{name}/json" if kind == "container" else f"/volumes/{name}"
            response = await executor._client.get(path)
            assert response.status_code == 404, (kind, name)
        print(
            json.dumps(
                {
                    "unauthorized_no_container": True,
                    "isolation": True,
                    "timeout": True,
                    "repeated_cancel_cleanup": True,
                    "symlink_artifact_rejected": True,
                    "created_resources_verified_removed": len(created),
                    "supplier_api_calls": 0,
                }
            )
        )
    finally:
        await client.close()
        await executor.close()


if __name__ == "__main__":
    asyncio.run(main())
