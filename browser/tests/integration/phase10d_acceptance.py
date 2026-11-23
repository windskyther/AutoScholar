"""Real cached-Docker isolation, timeout and cancellation checks; no paid APIs or .env."""

import argparse
import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from autoscholar.coding.sandbox import SandboxRunRequest
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources

ROOT = Path(__file__).resolve().parents[2]
GUARD = """import os
import socket
from pathlib import Path
assert os.getuid() == 65532
assert not Path('/var/run/docker.sock').exists()
assert not Path('/app/.env').exists()
assert not any(os.getenv(name) for name in ('LLM_API_KEY', 'TAVILY_API_KEY', 'OPENAI_API_KEY'))
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert int(Path('/sys/fs/cgroup/memory.max').read_text()) <= 2147483648
assert int(Path('/sys/fs/cgroup/pids.max').read_text()) <= 128
quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
assert int(quota) <= 2 * int(period)
mounts = Path('/proc/self/mountinfo').read_text().splitlines()
assert any(line.split()[4] == '/' and 'ro' in line.split()[5].split(',') for line in mounts)
assert any(
    line.split()[4] == '/datasets/mnist' and 'ro' in line.split()[5].split(',')
    for line in mounts
)
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('Sandbox unexpectedly has egress')
print('Isolation checks passed')
"""


async def docker(context: str, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "--context",
        context,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise RuntimeError("Scoped Docker inspection failed")
    return stdout.decode().strip()


async def no_owned_resources(context: str, project: str) -> None:
    selector = "label=autoscholar.evaluation=" + project
    assert not await docker(context, "ps", "-aq", "--filter", selector), "Owned containers remain"
    assert not await docker(context, "volume", "ls", "-q", "--filter", selector), (
        "Owned volumes remain"
    )


async def run(container: str) -> dict[str, object]:
    sandbox = DockerEvaluationSandbox(container)
    health = await sandbox.health()
    resources = sandbox_resources(health, dataset=True)
    assert sandbox.context is not None
    context = sandbox.context
    project = container.removesuffix("-sandbox-manager-1")
    await no_owned_resources(context, project)
    guarded = await sandbox.run(
        SandboxRunRequest(
            task_id="evald-guard-" + uuid4().hex[:20],
            action="run_python",
            path="guard.py",
            files={"guard.py": GUARD},
            timeout_seconds=15,
        )
    )
    assert guarded.status == "succeeded" and guarded.exit_code == 0
    await no_owned_resources(context, project)
    timed = await sandbox.run(
        SandboxRunRequest(
            task_id="evald-timeout-" + uuid4().hex[:20],
            action="run_python",
            path="wait.py",
            files={"wait.py": "import time\ntime.sleep(15)\n"},
            timeout_seconds=1,
        )
    )
    assert timed.status == "timed_out"
    await no_owned_resources(context, project)
    task_id = "evald-cancel-" + uuid4().hex[:20]
    pending = asyncio.create_task(
        sandbox.run(
            SandboxRunRequest(
                task_id=task_id,
                action="run_python",
                path="wait.py",
                files={"wait.py": "import time\ntime.sleep(30)\n"},
                timeout_seconds=40,
            )
        )
    )
    try:
        deadline = time.monotonic() + 15
        while not await docker(
            context,
            "ps",
            "-q",
            "--filter",
            "label=autoscholar.evaluation=" + project,
            "--filter",
            "name=autoscholar-" + task_id[:24],
        ):
            if time.monotonic() > deadline or pending.done():
                raise AssertionError("Cancellation target never entered running container")
            await asyncio.sleep(0.1)
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=30)
    await no_owned_resources(context, project)
    return {
        "schema_version": 1,
        "isolation": True,
        "timeout": True,
        "cancellation": True,
        "owned_resources_remaining": 0,
        "external_api_calls": 0,
        "resources": resources,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-container", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.sandbox_container))
    destination = ROOT / "data/validation" / ("phase10d-" + uuid4().hex)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Phase 10D real isolation/timeout/cancellation passed: {destination}")


if __name__ == "__main__":
    main()
