"""PostgreSQL-backed workflow worker: python -m autoscholar.orchestration.worker."""

import asyncio
import signal
from contextlib import suppress

import structlog

from autoscholar.core.config import get_settings
from autoscholar.main import create_app
from autoscholar.orchestration.durable import DurableService

logger = structlog.get_logger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    app = create_app(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    async with app.router.lifespan_context(app):
        durable: DurableService | None = app.state.durable_service
        if durable is None:
            raise RuntimeError("Workflow worker dependencies unavailable")
        while not stop.is_set():
            tick = asyncio.create_task(durable.tick())
            stopping = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({tick, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                tick.cancel()
            stopping.cancel()
            results = await asyncio.gather(tick, stopping, return_exceptions=True)
            if isinstance(results[0], Exception):
                logger.error("workflow_tick_failed", exception_type=type(results[0]).__name__)
            if not stop.is_set() and results[0] is not True:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.workflow_worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_worker())
