import time

from autoscholar.coding.sandbox import (
    SandboxExecutor,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.core.budget import consume, current_budget
from autoscholar.core.journal import external_operation


class BudgetedSandbox:
    def __init__(self, sandbox: SandboxExecutor) -> None:
        self.sandbox = sandbox

    async def health(self) -> SandboxHealth:
        budget = current_budget.get()
        if budget is not None:
            budget.check()
        return await self.sandbox.health()

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        consume("sandbox_runs")
        consume("tool_calls")
        budget = current_budget.get()
        if budget is not None:
            remaining = budget.limits.wall_seconds - (time.monotonic() - budget.started)
            request = request.model_copy(
                update={"timeout_seconds": min(request.timeout_seconds, max(1, int(remaining)))}
            )
        async with external_operation("sandbox:" + request.action):
            return await self.sandbox.run(request)

    async def close(self) -> None:
        await self.sandbox.close()
