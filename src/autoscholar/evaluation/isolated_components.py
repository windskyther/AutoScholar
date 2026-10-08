"""Explicitly injected sandbox support; never execute benchmark code on the host."""

import base64
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.coding.sandbox import (
    SandboxExecutor,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.evaluation.datasets import decode_json


def sandbox_resources(health: SandboxHealth, *, dataset: bool) -> dict[str, str]:
    if health.status != "ok" or not health.engine or not health.image or not health.image_sha256:
        raise ValueError("Sandbox must attest a cached Docker image digest")
    resources = {"sandbox_image": health.image_sha256}
    if dataset:
        if not health.mnist_dataset or health.dataset_id != "mnist" or not health.dataset_sha256:
            raise ValueError("Experiment evaluation requires the cached MNIST manifest")
        resources["dataset"] = health.dataset_sha256
    return resources


class ObservedSandbox:
    def __init__(self, sandbox: SandboxExecutor, resources: dict[str, str]) -> None:
        self.delegate = sandbox
        self.resources = resources
        self.runs: list[tuple[SandboxRunRequest, SandboxRunResult]] = []

    async def health(self) -> SandboxHealth:
        health = await self.delegate.health()
        if sandbox_resources(health, dataset="dataset" in self.resources) != self.resources:
            raise ValueError("Sandbox resources changed after evaluation preflight")
        return health

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        # All sources go only to the explicitly provisioned isolated component.
        result = await self.delegate.run(request)
        self.runs.append((request, result))
        return result

    async def close(self) -> None:
        # The CLI/caller owns the shared client; each case owns only its recording wrapper.
        pass


@asynccontextmanager
async def local_component_task(
    root: Path, objective: str, *, mode: Literal["coding", "experiment"]
) -> AsyncIterator[tuple[str, AgentTaskRepository, WorkspaceManager]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        store = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
        task_id = "evald-" + uuid4().hex
        await store.create_task(task_id=task_id, objective=objective, mode=mode)
        workspace = WorkspaceManager(root)
        yield task_id, store, workspace
    finally:
        await engine.dispose()


def oracle_json(result: SandboxRunResult) -> object:
    if result.status != "succeeded" or result.exit_code != 0 or result.truncated:
        raise ValueError("Independent oracle did not complete")
    if len(result.artifacts) != 1:
        raise ValueError("Oracle must collect exactly one result artifact")
    artifact = result.artifacts[0]
    if artifact.path != "outputs/eval_oracle.json" or artifact.size_bytes > 65536:
        raise ValueError("Invalid oracle artifact scope/size")
    raw = base64.b64decode(artifact.data_base64, validate=True)
    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ValueError("Oracle artifact integrity failure")
    return decode_json(raw)
