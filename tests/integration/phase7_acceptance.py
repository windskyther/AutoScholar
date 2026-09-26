"""Two-process Phase 7 acceptance: real PostgreSQL/Docker, zero external API requests.

Run --prepare, restart the API, then run --resume TASK_ID in a fresh container.
Only explicitly created task IDs are claimed; existing tasks and records are preserved.
"""

import argparse
import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from pydantic import SecretStr

from autoscholar.coding.sandbox import SandboxClient
from autoscholar.core.config import Settings
from autoscholar.main import create_app
from autoscholar.orchestration.approvals import ApprovalDecision
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.orchestration.smoke import FaultOnceSandbox, OfflineCoordinator


async def main(prepare: bool, task_id: str | None) -> None:
    token = secrets.token_urlsafe(32)
    settings = Settings().model_copy(
        update={
            "experiment_api_token": SecretStr(token),
            "workflow_approval_threshold": 0,
        }
    )
    sandbox = SandboxClient(
        settings.sandbox_manager_url, timeout_seconds=settings.experiment_timeout_seconds + 10
    )
    fault = FaultOnceSandbox(sandbox)
    app = create_app(settings, llm_provider=OfflineCoordinator(), sandbox_executor=fault)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://acceptance",
            headers={"Authorization": f"Bearer {token}"},
        ) as api,
        asyncio.timeout(240),
    ):
        durable: DurableService = app.state.durable_service
        if prepare:
            project = await api.post("/projects", json={"name": "Phase 7 offline acceptance"})
            project.raise_for_status()
            project_id = project.json()["id"]
            response = await api.post(
                "/agent/tasks",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "objective": "Validate seeded CPU MNIST comparison; no external research.",
                    "mode": "autonomous",
                    "project_id": project_id,
                    "experiment_specification": {
                        "epochs": 1,
                        "train_samples": 128,
                        "test_samples": 128,
                    },
                },
            )
            response.raise_for_status()
            task_id = response.json()["task_id"]
            assert await durable.tick(task_id)
            assert await durable.tick(task_id)
            snapshot, job = await durable.repository.snapshot(task_id)
            assert snapshot.results["code"]["status"] == "succeeded"
            assert job.usage["model_calls"] == 2
            await durable.repository.control(task_id, "pause")
            print(
                json.dumps(
                    {
                        "phase": "prepared",
                        "task_id": task_id,
                        "coding_child": snapshot.results["code"]["child_task_id"],
                        "status": "paused",
                        "external_api_calls": 0,
                    }
                ),
                flush=True,
            )
            return
        assert task_id is not None
        snapshot, job = await durable.repository.snapshot(task_id)
        assert job.status == "paused" and snapshot.project_id is not None
        initial_child = snapshot.results["code"]["child_task_id"]
        project_id = snapshot.project_id
        await durable.repository.control(task_id, "resume")
        approvals = 0
        for _ in range(30):
            await durable.tick(task_id)
            snapshot, job = await durable.repository.snapshot(task_id)
            if job.status == "awaiting_approval":
                pending = [
                    item
                    for item in await durable.approvals.list(task_id)
                    if item["status"] == "pending"
                ]
                assert len(pending) == 1
                item = pending[0]
                await durable.approvals.decide(
                    task_id,
                    item["id"],
                    ApprovalDecision(
                        action="approve",
                        operation_sha256=item["operation_sha256"],
                        reason="Offline acceptance; bounded real CPU sandbox only",
                    ),
                )
                _, stopped = await durable.repository.snapshot(task_id)
                assert stopped.status == "paused"
                assert not await durable.tick(task_id)
                await durable.repository.control(task_id, "resume")
                approvals += 1
            elif job.status != "queued":
                break
        assert job.status == "succeeded", (job.status, job.error_code)
        assert fault.injections == 1 and job.usage["training_runs"] == 2
        assert job.usage["model_calls"] == 5 and job.usage["replans"] == 1
        assert approvals == 2
        assert snapshot.results["code"]["child_task_id"] == initial_child
        verified = 0
        child = snapshot.results["train"]["child_task_id"]
        manifest = await api.get(f"/agent/tasks/{child}/artifacts")
        manifest.raise_for_status()
        for item in manifest.json()["items"]:
            response = await api.get(f"/agent/tasks/{child}/artifacts/{item['id']}")
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
            verified += 1
        assert verified == 10
        memories = await durable.memory.experiences(project_id)
        assert len(memories) == 1 and memories[0]["evidence"]["verified"]
        second, _ = await durable.submit(
            {
                "objective": "Recall prior repair",
                "project_id": project_id,
                "budget": {"model_calls": 1},
            },
            str(uuid4()),
        )
        await durable.tick(second)
        recalled, _ = await durable.repository.snapshot(second)
        assert recalled.memory_context["experiences"][0]["id"] == memories[0]["id"]
        await durable.repository.control(second, "cancel")

        # Real PostgreSQL row locks: only one independent worker may claim a queued task.
        concurrent, _ = await durable.submit({"objective": "Lease isolation"}, str(uuid4()))
        owners = [str(uuid4()), str(uuid4())]
        claims = await asyncio.gather(
            *(durable.repository.claim(owner, 30, concurrent) for owner in owners)
        )
        assert sum(item is not None for item in claims) == 1
        winner = next(index for index, item in enumerate(claims) if item is not None)
        claim = claims[winner]
        assert claim is not None
        state, row = await durable.repository.snapshot(concurrent)
        await durable.repository.save(
            concurrent,
            owners[winner],
            claim[1],
            state,
            row.usage,
            row.active_seconds,
            status="cancelled",
        )

        uncertain, _ = await durable.submit({"objective": "Uncertain request"}, str(uuid4()))
        claim = await durable.repository.claim(durable.owner, 30, uncertain)
        assert claim
        await durable.repository.begin(uncertain, durable.owner, claim[1], {"stage": "planner"})
        await durable.repository.journal(
            uncertain, durable.owner, claim[1], "test-call", "llm", True, {"model_calls": 1}, 1
        )
        async with durable.repository.sessions() as session:
            expired = await session.get(WorkflowJobRow, uncertain)
            assert expired
            expired.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        assert await durable.tick(uncertain)
        _, row = await durable.repository.snapshot(uncertain)
        assert row.status == "recovery_required" and row.usage["model_calls"] == 1
        await durable.repository.control(uncertain, "cancel")
        result: dict[str, Any] = {
            "acceptance": "passed",
            "task_id": task_id,
            "verified_artifacts": verified,
            "restart_reused_child": initial_child,
            "approvals": approvals,
            "training_runs": job.usage["training_runs"],
            "experience_id": memories[0]["id"],
            "concurrent_claim": "passed",
            "unknown_call_replay_blocked": True,
            "external_api_calls": 0,
        }
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", action="store_true")
    group.add_argument("--resume")
    args = parser.parse_args()
    asyncio.run(main(args.prepare, args.resume))
