import asyncio
from uuid import uuid4

from autoscholar.coding.sandbox import SandboxClient, SandboxRunRequest


async def main() -> None:
    client = SandboxClient("http://127.0.0.1:8090", timeout_seconds=30)
    task_id = f"smoke-{uuid4().hex[:12]}"
    try:
        isolation = await client.run(
            SandboxRunRequest(
                task_id=task_id,
                action="run_python",
                path="isolation.py",
                files={
                    "isolation.py": (
                        "import os\n"
                        "import socket\n"
                        "import torch\n"
                        "assert not any('API_KEY' in key or 'TOKEN' in key for key in os.environ)\n"
                        "try:\n"
                        "    open('/autoscholar-write-test', 'w').close()\n"
                        "except OSError:\n"
                        "    print('rootfs-read-only')\n"
                        "else:\n"
                        "    raise AssertionError('root filesystem is writable')\n"
                        "try:\n"
                        "    socket.create_connection(('1.1.1.1', 53), timeout=1)\n"
                        "except OSError:\n"
                        "    print('network-blocked')\n"
                        "else:\n"
                        "    raise AssertionError('network is available')\n"
                        "print('torch', torch.__version__)\n"
                    )
                },
                timeout_seconds=15,
            )
        )
        if isolation.status != "succeeded":
            raise RuntimeError(f"isolation smoke test failed: {isolation.stderr}")

        pytest_result = await client.run(
            SandboxRunRequest(
                task_id=task_id,
                action="run_pytest",
                files={"test_ok.py": "def test_ok():\n    assert 2 + 2 == 4\n"},
                timeout_seconds=15,
            )
        )
        if pytest_result.status != "succeeded":
            raise RuntimeError(f"pytest smoke test failed: {pytest_result.stderr}")

        timeout = await client.run(
            SandboxRunRequest(
                task_id=task_id,
                action="run_python",
                path="wait.py",
                files={"wait.py": "import time\ntime.sleep(5)\n"},
                timeout_seconds=1,
            )
        )
        if timeout.status != "timed_out":
            raise RuntimeError("sandbox timeout was not enforced")
        print("Phase 4 sandbox smoke test passed")
        print(isolation.stdout.strip())
        print(pytest_result.stdout.strip())
        print("timeout-enforced")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
