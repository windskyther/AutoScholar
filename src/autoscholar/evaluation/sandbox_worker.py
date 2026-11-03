"""One bounded stdio request inside the dedicated Linux controller, never on the host."""

import asyncio
import os
import re
import sys

from autoscholar.coding.sandbox import DockerLimits, DockerSandboxExecutor, SandboxRunRequest
from autoscholar.evaluation.datasets import decode_json

MAX_REQUEST_BYTES = 12 * 1024 * 1024


async def serve() -> int:
    owner = os.getenv("AUTOSCHOLAR_EVAL_OWNER", "")
    image = os.getenv("SANDBOX_IMAGE", "")
    if (
        sys.platform != "linux"
        or not re.fullmatch(r"autoscholar-eval-[a-z0-9-]{1,45}", owner)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
        or any(
            os.getenv(name)
            for name in (
                "LLM_API_KEY",
                "TAVILY_API_KEY",
                "SEMANTIC_SCHOLAR_API_KEY",
                "OPENAI_API_KEY",
            )
        )
    ):
        raise ValueError("Worker must be an isolated, pinned, provider-free controller")
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise ValueError("Invalid bounded worker request")
    payload = decode_json(raw)
    if not isinstance(payload, dict):
        raise ValueError("Worker request must be an object")
    executor = DockerSandboxExecutor(
        image=image,
        dataset_volume="autoscholar_mnist_data",
        dataset_ready_file="/datasets/mnist/.autoscholar-ready",
        limits=DockerLimits(memory_bytes=2 * 1024**3, nano_cpus=2_000_000_000, pids_limit=128),
        evaluation_owner=owner,
    )
    loop = asyncio.get_running_loop()
    disconnected: asyncio.Future[None] = loop.create_future()

    def eof() -> None:
        if not os.read(sys.stdin.fileno(), 1):
            loop.remove_reader(sys.stdin.fileno())
            if not disconnected.done():
                disconnected.set_result(None)

    loop.add_reader(sys.stdin.fileno(), eof)
    try:
        if payload.keys() == {"operation"} and payload["operation"] == "health":
            execution = asyncio.create_task(executor.health())
        elif payload.keys() == {"operation", "request"} and payload["operation"] == "run":
            execution = asyncio.create_task(
                executor.run(SandboxRunRequest.model_validate(payload["request"]))
            )
        else:
            raise ValueError("Unsupported worker operation")
        try:
            done, _ = await asyncio.wait(
                {execution, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if execution not in done:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                return 130
            result = await execution
            print(result.model_dump_json(), flush=True)
            return 0
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    finally:
        loop.remove_reader(sys.stdin.fileno())
        disconnected.cancel()
        await executor.close()


def main() -> int:
    try:
        return asyncio.run(serve())
    except (Exception, KeyboardInterrupt):
        # No Docker error bodies, sources or exception text cross this diagnostic boundary.
        print('{"worker_error":"execution_failed"}', flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
