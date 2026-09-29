"""Isolated acceptance worker process with an offline provider and no API credentials."""

import asyncio
import os
from typing import Any
from unittest.mock import patch

from autoscholar.coding.sandbox import SandboxClient, SandboxRunRequest, SandboxRunResult
from autoscholar.core.config import Settings
from autoscholar.llm import LLMResult
from autoscholar.main import create_app
from autoscholar.orchestration import worker
from autoscholar.orchestration.checkpoints import Snapshot
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.smoke import OfflineCoordinator


class AcceptanceCoordinator(OfflineCoordinator):
    async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
        if any(tool.name == "submit_task_plan" for tool in kwargs.get("tools", [])):
            if os.environ["ACCEPTANCE_CASE"] == "wait_llm":
                await asyncio.Event().wait()
            elif os.environ["ACCEPTANCE_CASE"] == "slow_llm":
                await asyncio.sleep(12)
        return await super().generate(messages, **kwargs)


class HeldExperimentClient(SandboxClient):
    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if request.collect_artifacts:
            # An actual isolated Python process, long enough to terminate its caller.
            # Never reports fabricated metrics or artifacts.
            request = request.model_copy(
                update={
                    "files": {"wait.py": "import time\ntime.sleep(60)\n"},
                    "path": "wait.py",
                    "args": [],
                    "timeout_seconds": 60,
                }
            )
        return await super().run(request)


async def main() -> None:
    if os.getenv("LLM_API_KEY") or os.getenv("TAVILY_API_KEY"):
        raise RuntimeError("Acceptance worker refuses API credentials")
    Settings.model_config["env_file"] = None
    settings = Settings()
    sandbox = HeldExperimentClient(settings.sandbox_manager_url, timeout_seconds=75)
    app = create_app(
        settings,
        llm_provider=AcceptanceCoordinator(),
        sandbox_executor=sandbox if os.environ["ACCEPTANCE_CASE"] == "hold_training" else None,
    )
    target = os.environ["ACCEPTANCE_TASK_ID"]

    class ScopedWorker(DurableService):
        announced = False

        async def tick(self, task_id: str | None = None) -> bool:
            if not self.announced:
                print("ACCEPTANCE_WORKER_READY", flush=True)
                self.announced = True
            return await super().tick(target)

    durable = ScopedWorker(app.state.autonomous_service, lease_seconds=6)
    if os.environ["ACCEPTANCE_CASE"] == "hold_checkpoint":
        original_save = durable.repository.save

        async def held_save(*args: Any, **kwargs: Any) -> None:
            snapshot: Snapshot = args[3]
            if "code" in snapshot.results:
                await asyncio.Event().wait()
            await original_save(*args, **kwargs)

        durable.repository.save = held_save  # type: ignore[method-assign]
    app.state.durable_service = durable
    try:
        with (
            patch.object(worker, "create_app", return_value=app),
            patch.object(worker, "get_settings", return_value=settings),
        ):
            await worker.run_worker()
    finally:
        await sandbox.close()


if __name__ == "__main__":
    asyncio.run(main())
