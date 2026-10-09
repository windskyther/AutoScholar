"""Cancel an actually running E2E training container; inspect only this evaluation's resources."""

import argparse
import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import create_async_engine

from autoscholar.agent.database_models import TaskStepRow
from autoscholar.coding.sandbox import SandboxHealth, SandboxRunRequest, SandboxRunResult
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.e2e_adapter import InjectedWorkflowAdapter
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.runner import _git_state
from autoscholar.evaluation.workflow_fixture import WorkflowFixture
from autoscholar.orchestration.durable_models import WorkflowJobRow

ROOT = Path(__file__).resolve().parents[2]


class SignalledSandbox:
    def __init__(self, sandbox: DockerEvaluationSandbox) -> None:
        self.sandbox = sandbox
        self.training = asyncio.Event()
        self.request: SandboxRunRequest | None = None

    async def health(self) -> SandboxHealth:
        return await self.sandbox.health()

    async def close(self) -> None:
        pass

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        if request.action == "run_python" and request.path == "train.py":
            self.request = request
            self.training.set()
        return await self.sandbox.run(request)


async def no_owned_resources(sandbox: DockerEvaluationSandbox, project: str) -> None:
    selector = "label=autoscholar.evaluation=" + project
    assert not (
        await sandbox._metadata(
            "--context",
            sandbox.context or "",
            "ps",
            "-aq",
            "--filter",
            selector,
        )
    ).strip(), "Evaluation child containers remain"
    assert not (
        await sandbox._metadata(
            "--context",
            sandbox.context or "",
            "volume",
            "ls",
            "-q",
            "--filter",
            selector,
        )
    ).strip(), "Evaluation child volumes remain"


async def run(container: str) -> dict[str, object]:
    sandbox = DockerEvaluationSandbox(container)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    project = container.removesuffix("-sandbox-manager-1")
    await no_owned_resources(sandbox, project)
    workspace_root = (ROOT / "data/evaluation/workflow-workspaces").resolve()
    if not workspace_root.is_relative_to((ROOT / "data/evaluation").resolve()):
        raise ValueError("Workflow test state escaped its local output scope")
    before = set(workspace_root.glob("evale-*/workflow.sqlite"))
    fixture, digest = load_fixture(
        ROOT / "benchmarks/end_to_end/workflow_fixture_v1.json", WorkflowFixture
    )
    signalled = SignalledSandbox(sandbox)
    adapter = InjectedWorkflowAdapter(
        fixture,
        fixture_sha256=digest,
        sandbox=signalled,
        resources=resources,
        workspace_root=workspace_root,
    )
    execution = asyncio.create_task(
        adapter.execute(fixture.scripts["normal"].prompt, {"query_id": "normal"}, seed=42)
    )
    try:
        await asyncio.wait_for(signalled.training.wait(), timeout=120)
        assert signalled.request is not None
        deadline = time.monotonic() + 15
        while not (
            await sandbox._metadata(
                "--context",
                sandbox.context or "",
                "ps",
                "-q",
                "--filter",
                "label=autoscholar.evaluation=" + project,
                "--filter",
                "name=autoscholar-" + signalled.request.task_id[:24],
            )
        ).strip():
            if time.monotonic() > deadline or execution.done():
                raise AssertionError("E2E cancellation target never entered a running container")
            await asyncio.sleep(0.1)
        execution.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(execution, timeout=40)
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await sandbox.close()
    await no_owned_resources(sandbox, project)
    created = set(workspace_root.glob("evale-*/workflow.sqlite")) - before
    assert len(created) == 1
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(created.pop())))
    try:
        async with engine.connect() as connection:
            jobs = (
                await connection.execute(
                    select(WorkflowJobRow.status, WorkflowJobRow.pending_calls)
                )
            ).all()
            training_steps = (
                await connection.execute(
                    select(TaskStepRow.step_id).where(TaskStepRow.step_id == "train")
                )
            ).all()
        assert len(jobs) == 1 and jobs[0].status == "recovery_required" and jobs[0].pending_calls
        assert len(training_steps) == 1
    finally:
        await engine.dispose()
    revision, dirty = _git_state(ROOT)
    return {
        "schema_version": 1,
        "git_revision": revision,
        "git_dirty": dirty,
        "real_training_entered": True,
        "e2e_cancellation": True,
        "interrupted_unit_requires_recovery": True,
        "training_not_automatically_replayed": True,
        "database_reopened_after_cancel": True,
        "owned_resources_remaining": 0,
        "external_api_calls": 0,
        "resources": resources,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-container", required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.sandbox_container))
    destination = ROOT / "data/validation" / ("phase10e-" + uuid4().hex)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Phase 10E real E2E cancellation/cleanup passed: {destination}")


if __name__ == "__main__":
    main()
