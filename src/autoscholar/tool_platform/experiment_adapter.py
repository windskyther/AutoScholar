"""Core sandbox facade, with one budget/journal boundary for submit + polling."""

import asyncio
import time
from contextlib import suppress
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from autoscholar.coding.sandbox import (
    SandboxError,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import current_budget, current_parent
from autoscholar.core.journal import external_operation
from autoscholar.tool_platform.artifact_spool import ArtifactSpool
from autoscholar.tool_platform.context import (
    ToolScope,
    current_tool_scope,
    current_workflow_claim,
    tool_scope,
)
from autoscholar.tool_platform.experiment_contracts import REPLY
from autoscholar.tool_platform.experiment_service import source_hash
from autoscholar.tool_platform.gateway import ToolGateway, ToolGatewayError, argument_digest
from autoscholar.tool_platform.registry import InvocationRegistry


class MCPExperimentSandbox:
    owns_journal = True

    def __init__(
        self,
        gateway: ToolGateway,
        workspace: WorkspaceManager,
        spool: ArtifactSpool,
        registry: InvocationRegistry,
        *,
        poll_seconds: float = 0.5,
    ) -> None:
        self.gateway, self.workspace, self.spool, self.registry = (
            gateway,
            workspace,
            spool,
            registry,
        )
        self.poll_seconds = poll_seconds

    async def health(self) -> SandboxHealth:
        try:
            return SandboxHealth.model_validate(
                await self.gateway.invoke("sandbox_health", {}, journal=False)
            )
        except (ToolGatewayError, ValueError):
            return SandboxHealth(status="error", engine=False, image=False, mnist_dataset=False)

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if self.workspace.source_snapshot(request.task_id) != request.files:
            raise SandboxError("experiment_source_changed", "The submitted source snapshot changed")
        prior_scope = current_tool_scope.get()
        if prior_scope is not None and prior_scope.task_id != request.task_id:
            raise SandboxError("experiment_scope_mismatch", "The execution task scope changed")
        with tool_scope(prior_scope or ToolScope(request.task_id)):
            return await self._run(request)

    async def _run(self, request: SandboxRunRequest) -> SandboxRunResult:
        health = await self.health() if request.collect_artifacts else None
        arguments = request.model_dump(exclude={"files", "task_id"})
        arguments.update(
            source_sha256=source_hash(request.files),
            dataset_sha256=health.dataset_sha256 if health else None,
        )
        operation_id = str(uuid4())
        claim, scope = current_workflow_claim.get(), current_tool_scope.get()
        assert scope is not None
        context: dict[str, Any] = {
            "operation_id": operation_id,
            "parent_task_id": current_parent.get(),
            "tool": "execute",
            "arguments_sha256": argument_digest(arguments),
            "deadline": time.time() + request.timeout_seconds + 10,
            "scope": scope.payload(),
            "claim": asdict(claim) if claim else None,
        }
        await self.registry.prepare("experiment", context, REPLY)
        try:
            async with external_operation("mcp:execute", operation_id=operation_id):
                response = await self.gateway.invoke(
                    "execute", arguments, operation_id=operation_id, journal=False
                )
                async with asyncio.timeout(request.timeout_seconds + 12):
                    while response["status"] in {"queued", "running"}:
                        await asyncio.sleep(self.poll_seconds)
                        budget = current_budget.get()
                        if budget is not None:
                            budget.check()
                        response = await self.gateway.invoke(
                            "get_status", {"operation_id": operation_id}, journal=False
                        )
                if response["status"] == "completed":
                    result = self.spool.load(
                        operation_id, response["result"], request.collect_artifacts
                    )
                elif response["status"] == "denied":
                    result = None
                else:
                    raise ToolGatewayError("experiment_result_uncertain", uncertain=True)
                await self.registry.complete(context, response)
            if result is None:
                raise SandboxError(
                    response["error_code"] or "experiment_denied",
                    "Experiment submission was rejected",
                )
            return result
        except BaseException:
            # Best-effort stop only, never retry execute or suppress the original failure.
            with suppress(BaseException):
                async with asyncio.timeout(3):
                    await self.gateway.invoke(
                        "cancel", {"operation_id": operation_id}, journal=False
                    )
            raise

    async def close(self) -> None:
        # Streamable HTTP connections are scoped to each bounded gateway request.
        pass
