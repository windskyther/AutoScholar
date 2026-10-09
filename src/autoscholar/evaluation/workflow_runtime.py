"""Per-case file-backed workflow state; never connect to the application database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.agent.runner import AgentRunner
from autoscholar.coding.agent import CodingAgent, CodingLimits
from autoscholar.coding.sandbox import SandboxExecutor
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import BudgetedLLM
from autoscholar.evaluation.workflow_fixture import (
    ScriptedWorkflowProvider,
    WorkflowScript,
    WorkflowSearch,
)
from autoscholar.experiment.artifacts import ArtifactManager
from autoscholar.experiment.service import ExperimentService
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.repository import WorkflowRepository
from autoscholar.orchestration.sandbox import BudgetedSandbox
from autoscholar.orchestration.service import AutonomousService


@dataclass
class LocalWorkflow:
    database: Path
    engine: AsyncEngine
    tasks: AgentTaskRepository
    workspace: WorkspaceManager

    async def reopen(self) -> None:
        # Called only BETWEEN committed units: no call or worker is replayed implicitly.
        await self.engine.dispose()
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(self.database))
        )
        self.tasks = AgentTaskRepository(async_sessionmaker(self.engine, expire_on_commit=False))


@asynccontextmanager
async def local_workflow(root: Path) -> AsyncIterator[LocalWorkflow]:
    destination = root / ("evale-" + uuid4().hex)
    destination.mkdir(parents=True, exist_ok=False)
    database = destination / "workflow.sqlite"
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(database)))
    state = LocalWorkflow(
        database=database,
        engine=engine,
        tasks=AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False)),
        workspace=WorkspaceManager(destination / "workspaces"),
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield state
    finally:
        await state.engine.dispose()


def workflow_service(
    state: LocalWorkflow,
    provider: ScriptedWorkflowProvider,
    search: WorkflowSearch,
    sandbox: SandboxExecutor,
    script: WorkflowScript,
) -> DurableService:
    metered = BudgetedLLM(provider)
    isolated = BudgetedSandbox(sandbox)
    artifacts = ArtifactManager(state.workspace, state.tasks)
    coding = CodingAgent(
        provider=metered,
        repository=state.tasks,
        workspaces=state.workspace,
        sandbox=isolated,
        limits=CodingLimits(timeout_seconds=30, max_repairs=0),
    )
    runner = AgentRunner(
        provider=metered,
        repository=state.tasks,
        tools=[],
        coding_service=coding,
        research_services=[search],
    )
    service = AutonomousService(
        provider=metered,
        tasks=state.tasks,
        workflows=WorkflowRepository(state.tasks.session_factory),
        runner=runner,
        coding=coding,
        workspace=state.workspace,
        artifacts=artifacts,
        experiments=ExperimentService(
            repository=state.tasks,
            workspace=state.workspace,
            sandbox=isolated,
            artifacts=artifacts,
            timeout_seconds=60,
            max_repairs=0,
        ),
        limits=script.limits,
    )
    return DurableService(service)
