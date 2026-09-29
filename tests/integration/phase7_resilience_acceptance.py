"""Real process/network fault acceptance in an isolated, credential-free Docker stack.

Run in a disposable sandbox-manager container with this file and the worker helper mounted.
Creates a private PostgreSQL, manager, worker processes, network and workspace volume.
Never pauses production PostgreSQL or claims production tasks; removes only owned resources.
"""

import asyncio
import io
import json
import os
import tarfile
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from autoscholar.agent.database_models import Base
from autoscholar.core.config import Settings
from autoscholar.main import create_app
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowEventRow
from autoscholar.orchestration.smoke import OfflineCoordinator


class Harness:
    def __init__(self) -> None:
        self.tag = "phase7-fault-" + uuid4().hex[:10]
        self.image = "autoscholar-api:phase7-hardening"
        self.client = httpx.AsyncClient(
            base_url="http://docker",
            timeout=20,
            transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
        )
        self.containers: list[str] = []
        self.sandboxes: dict[str, str] = {}
        self.sandbox_mounts: dict[str, str] = {}
        self.sandbox_volumes: set[str] = set()
        self.network: str | None = None
        self.workspace: str | None = None
        self.postgres = ""
        self.database_url = ""
        self.manager_url = ""
        self.worker_source = Path("/tmp/phase7_resilience_worker.py").read_bytes()

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def container(
        self,
        name: str,
        command: list[str],
        env: dict[str, str],
        *,
        image: str | None = None,
        mounts: list[dict[str, Any]] | None = None,
        helper: bool = False,
    ) -> str:
        created = await self.call(
            "POST",
            "/containers/create",
            params={"name": self.tag + name},
            json={
                "Image": image or self.image,
                "Cmd": command,
                "Env": [f"{key}={value}" for key, value in env.items()],
                "Labels": {"autoscholar.acceptance": self.tag},
                "HostConfig": {
                    "NetworkMode": self.network,
                    "Mounts": mounts or [],
                    "Memory": 1024**3,
                    "PidsLimit": 256,
                },
            },
        )
        container_id = str(created["Id"])
        self.containers.append(container_id)
        if helper:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                info = tarfile.TarInfo("phase7_resilience_worker.py")
                info.size, info.mode = len(self.worker_source), 0o444
                archive.addfile(info, io.BytesIO(self.worker_source))
            await self.call(
                "PUT",
                f"/containers/{container_id}/archive",
                params={"path": "/tmp"},
                content=buffer.getvalue(),
                headers={"Content-Type": "application/x-tar"},
            )
        await self.call("POST", f"/containers/{container_id}/start")
        return container_id

    async def setup(self) -> Settings:
        network = await self.call(
            "POST",
            "/networks/create",
            json={
                "Name": self.tag,
                "Internal": True,
                "Labels": {"autoscholar.acceptance": self.tag},
            },
        )
        self.network = network["Id"]
        # Attach only this disposable acceptance runner, not the production manager.
        runner = await self.call("GET", f"/containers/{os.environ['HOSTNAME']}/json")
        assert "-run-" in runner["Name"], "Run acceptance in a disposable Compose run container"
        await self.call(
            "POST", f"/networks/{self.network}/connect", json={"Container": runner["Id"]}
        )
        self.workspace = self.tag + "-workspace"
        await self.call(
            "POST",
            "/volumes/create",
            json={
                "Name": self.workspace,
                "Labels": {"autoscholar.acceptance": self.tag},
            },
        )
        self.postgres = await self.container(
            "-postgres",
            ["postgres"],
            {
                "POSTGRES_DB": "acceptance",
                "POSTGRES_USER": "acceptance",
                "POSTGRES_PASSWORD": "isolated-acceptance-only",
            },
            image="postgres:16-alpine",
        )
        self.database_url = (
            "postgresql+asyncpg://acceptance:isolated-acceptance-only@"
            + self.tag
            + "-postgres:5432/acceptance"
        )
        self.manager_url = "http://" + self.tag + "-manager:8090"
        await self.container(
            "-manager",
            ["uvicorn", "autoscholar.sandbox.manager:app", "--host", "0.0.0.0", "--port", "8090"],
            {
                "SANDBOX_IMAGE": "autoscholar-python-sandbox:phase4",
                "SANDBOX_DATASET_VOLUME": "autoscholar_mnist_data",
            },
            mounts=[
                {
                    "Type": "bind",
                    "Source": "/var/run/docker.sock",
                    "Target": "/var/run/docker.sock",
                },
                {
                    "Type": "volume",
                    "Source": "autoscholar_mnist_data",
                    "Target": "/datasets/mnist",
                    "ReadOnly": True,
                },
            ],
        )
        Settings.model_config["env_file"] = None
        return Settings(
            database_url=self.database_url,
            sandbox_manager_url=self.manager_url,
            log_level="ERROR",
            experiment_api_token=SecretStr("isolated-acceptance-token"),
            workflow_job_lease_seconds=6,
        )

    async def actor(self, task_id: str, scenario: str = "normal") -> str:
        assert self.workspace
        actor = await self.container(
            "-worker-" + uuid4().hex[:8],
            ["python", "/tmp/phase7_resilience_worker.py"],
            {
                "DATABASE_URL": self.database_url,
                "SANDBOX_MANAGER_URL": self.manager_url,
                "WORKSPACE_ROOT": "/data/workspaces",
                "WORKFLOW_JOB_LEASE_SECONDS": "6",
                "WORKFLOW_WORKER_POLL_SECONDS": "0.2",
                "LOG_LEVEL": "ERROR",
                "ACCEPTANCE_TASK_ID": task_id,
                "ACCEPTANCE_CASE": scenario,
            },
            mounts=[{"Type": "volume", "Source": self.workspace, "Target": "/data/workspaces"}],
            helper=True,
        )

        async def ready() -> bool:
            response = await self.client.get(
                f"/containers/{actor}/logs", params={"stdout": "true", "stderr": "true"}
            )
            response.raise_for_status()
            return b"ACCEPTANCE_WORKER_READY" in response.content

        await until(ready)
        return actor

    async def signal(self, container_id: str, signal: str) -> None:
        assert container_id in self.containers
        await self.call("POST", f"/containers/{container_id}/kill", params={"signal": signal})
        if signal == "SIGTERM":

            async def stopped() -> bool:
                state = await self.call("GET", f"/containers/{container_id}/json")
                if state["State"]["Running"]:
                    return False
                assert state["State"]["ExitCode"] == 0, state["State"]
                return True

            await until(stopped, seconds=20)

    async def find_sandbox(self, child_id: str) -> bool:
        prefix = "/autoscholar-" + child_id[:24] + "-"
        containers = await self.call(
            "GET",
            "/containers/json",
            params={"all": "true", "filters": json.dumps({"name": [prefix]})},
        )
        for container in containers:
            assert any(name.startswith(prefix) for name in container["Names"])
            inspected = await self.call("GET", f"/containers/{container['Id']}/json")
            if inspected["Config"]["Cmd"] != ["python", "wait.py"]:
                continue
            self.sandboxes[container["Id"]] = prefix
            for mount in inspected["Mounts"]:
                if mount["Destination"] == "/workspace":
                    self.sandbox_volumes.add(mount["Name"])
                    self.sandbox_mounts[container["Id"]] = mount["Name"]
            return inspected["State"]["Running"] is True
        return False

    async def sandbox_gone(self, container_id: str) -> bool:
        assert container_id in self.sandboxes
        container = await self.client.get(f"/containers/{container_id}/json")
        volume = await self.client.get(f"/volumes/{self.sandbox_mounts[container_id]}")
        return container.status_code == volume.status_code == 404

    async def cleanup(self) -> None:
        errors: list[str] = []
        # Validate ownership on every deletion, including cleanup after a failed assertion.
        for container_id in reversed(self.containers):
            try:
                response = await self.client.get(f"/containers/{container_id}/json")
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                state = response.json()
                assert state["Config"]["Labels"]["autoscholar.acceptance"] == self.tag
                if state["State"].get("Paused"):
                    await self.call("POST", f"/containers/{container_id}/unpause")
                await self.call(
                    "DELETE", f"/containers/{container_id}", params={"force": "true", "v": "true"}
                )
            except Exception as exc:
                errors.append(f"container:{container_id}:{type(exc).__name__}")
        for container_id, prefix in self.sandboxes.items():
            response = await self.client.get(f"/containers/{container_id}/json")
            if response.status_code != 404:
                response.raise_for_status()
                assert response.json()["Name"].startswith(prefix)
                await self.call("DELETE", f"/containers/{container_id}", params={"force": "true"})
        for volume in [*self.sandbox_volumes, *([self.workspace] if self.workspace else [])]:
            response = await self.client.get(f"/volumes/{volume}")
            if response.status_code != 404:
                response.raise_for_status()
                labels = response.json()["Labels"]
                assert labels.get("autoscholar.acceptance") == self.tag or (
                    volume in self.sandbox_volumes and labels.get("autoscholar.temporary") == "true"
                )
                await self.call("DELETE", f"/volumes/{volume}")
        if self.network:
            network = await self.call("GET", f"/networks/{self.network}")
            assert network["Labels"]["autoscholar.acceptance"] == self.tag
            for container_id in network.get("Containers", {}):
                # Only this test's disposable runner may remain connected.
                assert container_id.startswith(os.environ["HOSTNAME"])
                await self.call(
                    "POST", f"/networks/{self.network}/disconnect", json={"Container": container_id}
                )
            await self.call("DELETE", f"/networks/{self.network}")
        await self.client.aclose()
        assert not errors, errors


async def until(predicate: Callable[[], Awaitable[bool]], *, seconds: float = 45) -> None:
    async with asyncio.timeout(seconds):
        while True:
            try:
                if await predicate():
                    return
            except (SQLAlchemyError, OSError):
                # A just-restarted isolated PostgreSQL may still reject connections.
                pass
            await asyncio.sleep(0.1)


async def scenarios(
    harness: Harness, durable: DurableService, api: httpx.AsyncClient
) -> dict[str, Any]:
    recorded: dict[str, Any] = {}

    async def submit(name: str, *, approval: bool = False) -> str:
        print(json.dumps({"scenario_started": name}), flush=True)
        submitter = DurableService(durable.service, approval_threshold=0) if approval else durable
        task_id, _ = await submitter.submit(
            {
                "objective": "Offline Phase 7 fault acceptance: " + name,
                "experiment_specification": {
                    "epochs": 1,
                    "train_samples": 128,
                    "test_samples": 128,
                },
            },
            str(uuid4()),
        )
        return task_id

    async def has_status(task_id: str, status: str) -> bool:
        _, row = await durable.repository.snapshot(task_id)
        return row.status == status

    async def pending(task_id: str, kind: str) -> bool:
        _, row = await durable.repository.snapshot(task_id)
        return kind in row.pending_calls.values()

    # Actual graceful shutdown and SIGKILL, while an offline model request is outstanding.
    for signal in ("SIGTERM", "SIGKILL"):
        task_id = await submit(signal)
        actor = await harness.actor(task_id, "wait_llm")
        await until(partial(pending, task_id, "llm"))
        await harness.signal(actor, signal)
        replacement = await harness.actor(task_id)
        await until(partial(has_status, task_id, "recovery_required"))
        _, row = await durable.repository.snapshot(task_id)
        assert row.usage["model_calls"] == 1 and row.pending_calls
        assert not await durable.service.workflows.history(task_id, "plans")
        await harness.signal(replacement, "SIGTERM")
        recorded[signal] = {"task_id": task_id, "status": row.status, "model_calls": 1}

    # Kill after a real coding child committed, but before its parent checkpoint commits.
    task_id = await submit("completed coding checkpoint gap")
    actor = await harness.actor(task_id, "hold_checkpoint")

    async def code_committed() -> bool:
        steps = await durable.service.workflows.history(task_id, "steps")
        return any(step["step_id"] == "code" and step["status"] == "succeeded" for step in steps)

    await until(code_committed)
    await harness.signal(actor, "SIGKILL")
    replacement = await harness.actor(task_id)
    await until(lambda: has_status(task_id, "succeeded"), seconds=90)
    snapshot, row = await durable.repository.snapshot(task_id)
    steps = await durable.service.workflows.history(task_id, "steps")
    assert len([step for step in steps if step["step_id"] == "code"]) == 1
    assert row.usage["model_calls"] == 3 and row.usage["training_runs"] == 1
    assert (
        len(
            await durable.service.tasks.list_artifacts(
                snapshot.results["train"]["child_task_id"],
            )
        )
        == 10
    )
    await harness.signal(replacement, "SIGTERM")
    recorded["checkpoint_gap"] = {"task_id": task_id, "coding_runs": 1, "training_runs": 1}

    # Old owner resumes after an actual process suspension and a new lease generation.
    task_id = await submit("lease takeover")
    actor = await harness.actor(task_id, "slow_llm")
    await until(lambda: pending(task_id, "llm"))
    await harness.signal(actor, "SIGSTOP")
    replacement = await harness.actor(task_id)
    await until(lambda: has_status(task_id, "recovery_required"))
    before, old_row = await durable.repository.snapshot(task_id)
    await harness.signal(actor, "SIGCONT")
    await asyncio.sleep(8)
    after, row = await durable.repository.snapshot(task_id)
    assert row.status == "recovery_required" and row.usage == old_row.usage
    assert row.checkpoint_sequence == old_row.checkpoint_sequence and before == after
    assert not await durable.service.workflows.history(task_id, "plans")
    await harness.signal(actor, "SIGTERM")
    await harness.signal(replacement, "SIGTERM")
    recorded["lease_takeover"] = {
        "task_id": task_id,
        "generation": row.generation,
        "stale_commit_blocked": True,
    }

    # Worker dies/loses PostgreSQL while a real sandbox Python process is running.
    for failure in ("kill_training", "database_unresponsive", "database_disconnect"):
        task_id = await submit(failure)
        actor = await harness.actor(task_id, "hold_training")
        await until(partial(pending, task_id, "sandbox:run_python"))
        steps = await durable.service.workflows.history(task_id, "steps")
        child_id = next(step["child_task_id"] for step in steps if step["step_id"] == "train")
        await until(partial(harness.find_sandbox, child_id))
        sandbox_id = next(
            cid
            for cid, prefix in harness.sandboxes.items()
            if prefix == "/autoscholar-" + child_id[:24] + "-"
        )
        started = asyncio.get_running_loop().time()
        if failure == "kill_training":
            await harness.signal(actor, "SIGKILL")
        elif failure == "database_unresponsive":
            await harness.call("POST", f"/containers/{harness.postgres}/pause")
        else:
            await harness.signal(harness.postgres, "SIGKILL")
        try:
            await until(partial(harness.sandbox_gone, sandbox_id), seconds=12)
        except TimeoutError:
            print(json.dumps({"cleanup_timeout": failure, "task_id": task_id}), flush=True)
            response = await harness.client.get(
                f"/containers/{actor}/logs", params={"stdout": "true", "stderr": "true"}
            )
            print(response.content.decode(errors="replace"), flush=True)
            raise
        finally:
            if failure == "database_unresponsive":
                await harness.call("POST", f"/containers/{harness.postgres}/unpause")
            elif failure == "database_disconnect":
                await harness.call("POST", f"/containers/{harness.postgres}/start")
        cleaned_seconds = asyncio.get_running_loop().time() - started
        if failure != "kill_training":
            await harness.signal(actor, "SIGTERM")
        replacement = await harness.actor(task_id)
        await until(partial(has_status, task_id, "recovery_required"))
        _, row = await durable.repository.snapshot(task_id)
        assert row.usage["training_runs"] == 1 and row.usage["model_calls"] == 2
        assert (
            row.pending_calls
            and len(await durable.service.workflows.history(task_id, "steps")) == 2
        )
        await harness.signal(replacement, "SIGTERM")
        recorded[failure] = {
            "task_id": task_id,
            "cleanup_seconds": round(cleaned_seconds, 2),
            "training_runs": 1,
            "status": row.status,
        }

    # Real PostgreSQL transactions and actual API routes under conflicting decisions.
    for race in ("approve_twice", "approve_modify", "approve_cancel", "resume_twice"):
        task_id = await submit(race, approval=True)
        actor = await harness.actor(task_id)
        await until(partial(has_status, task_id, "awaiting_approval"))
        approval = (await durable.approvals.list(task_id))[0]
        endpoint = f"/agent/tasks/{task_id}/approvals/{approval['id']}/decision"
        approve = {"action": "approve", "operation_sha256": approval["operation_sha256"]}
        if race == "approve_modify":
            modify = approve | {
                "action": "modify",
                "specification": {"epochs": 2, "train_samples": 128, "test_samples": 128},
            }
            responses = await asyncio.gather(
                api.post(endpoint, json=approve), api.post(endpoint, json=modify)
            )
            assert sorted(response.status_code for response in responses) == [200, 409]
            assert (await durable.repository.snapshot(task_id))[1].status == "paused"
        elif race == "approve_cancel":
            responses = await asyncio.gather(
                api.post(endpoint, json=approve), api.post(f"/agent/tasks/{task_id}/cancel")
            )
            assert all(response.status_code in {200, 409} for response in responses)
            assert (await durable.repository.snapshot(task_id))[1].status == "cancelled"
        else:
            responses = await asyncio.gather(
                api.post(endpoint, json=approve), api.post(endpoint, json=approve)
            )
            assert all(response.status_code == 200 for response in responses)
            if race == "resume_twice":
                second_worker = await harness.actor(task_id)
                responses = await asyncio.gather(
                    api.post(f"/agent/tasks/{task_id}/resume"),
                    api.post(f"/agent/tasks/{task_id}/resume"),
                )
                assert all(response.status_code == 200 for response in responses)
                await until(partial(has_status, task_id, "succeeded"), seconds=90)
                _, row = await durable.repository.snapshot(task_id)
                assert row.usage["training_runs"] == 1
                async with durable.repository.sessions() as session:
                    events = (
                        await session.scalars(
                            select(WorkflowEventRow).where(
                                WorkflowEventRow.task_id == task_id,
                                WorkflowEventRow.kind == "approval_consumed",
                            )
                        )
                    ).all()
                assert len(events) == 1
                await harness.signal(second_worker, "SIGTERM")
            else:
                assert (await durable.repository.snapshot(task_id))[1].status == "paused"
        await harness.signal(actor, "SIGTERM")
        snapshot, row = await durable.repository.snapshot(task_id)
        if race != "resume_twice":
            assert row.usage.get("training_runs", 0) == 0
        recorded[race] = {
            "task_id": task_id,
            "status": row.status,
            "http_codes": [response.status_code for response in responses],
            "training_runs": row.usage.get("training_runs", 0),
        }
    return recorded


async def main() -> None:
    if os.getenv("LLM_API_KEY") or os.getenv("TAVILY_API_KEY"):
        raise RuntimeError("Acceptance runner refuses API credentials")
    harness = Harness()
    try:
        settings = await harness.setup()
        app = create_app(settings, llm_provider=OfflineCoordinator())
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://acceptance",
                headers={"Authorization": "Bearer isolated-acceptance-token"},
            ) as api,
        ):

            async def ready() -> bool:
                try:
                    return bool(await app.state.database.ping())
                except Exception:
                    return False

            await until(ready)
            # This database is disposable and isolated; production migrations are not modified.
            async with app.state.database._engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            async def manager_ready() -> bool:
                return bool((await app.state.sandbox_executor.health()).mnist_dataset)

            await until(manager_ready)
            result = await scenarios(harness, app.state.durable_service, api)
            print(
                json.dumps({"acceptance": "passed", "external_api_calls": 0, "scenarios": result}),
                flush=True,
            )
    finally:
        await harness.cleanup()
        print(json.dumps({"cleanup": "passed", "isolated_stack": harness.tag}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
