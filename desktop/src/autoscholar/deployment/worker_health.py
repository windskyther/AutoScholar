import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

PATH = Path("/tmp/autoscholar-worker.json")


@asynccontextmanager
async def heartbeat() -> AsyncIterator[None]:
    # Native Windows development has no Docker health probe. In particular,
    # os.kill(pid, 0) has different semantics there and must never be used.
    if sys.platform == "win32":
        yield
        return

    async def update() -> None:
        while True:
            PATH.write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="ascii")
            await asyncio.sleep(10)

    task = asyncio.create_task(update())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        PATH.unlink(missing_ok=True)


def main() -> None:
    if sys.platform == "win32":
        raise SystemExit(1)
    value = json.loads(PATH.read_text(encoding="ascii"))
    if not 0 <= time.time() - value["at"] < 120:
        raise SystemExit(1)
    os.kill(value["pid"], 0)


if __name__ == "__main__":
    main()
