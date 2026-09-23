"""Real PostgreSQL/Docker concurrent workflows with a scripted, zero-cost coordinator."""

import asyncio
import hashlib
import json
import secrets
from typing import Any

import httpx
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.llm import LLMResult
from autoscholar.main import create_app
from autoscholar.orchestration.smoke import OfflineCoordinator


class SynchronizedCoordinator(OfflineCoordinator):
    def __init__(self) -> None:
        self.barrier = asyncio.Barrier(2)

    async def generate(self, messages: Any, **kwargs: Any) -> LLMResult:
        if any(tool.name == "submit_task_plan" for tool in kwargs.get("tools", [])):
            await asyncio.wait_for(self.barrier.wait(), timeout=15)
        return await super().generate(messages, **kwargs)


async def main() -> None:
    token = secrets.token_urlsafe(32)
    settings = Settings().model_copy(update={"experiment_api_token": SecretStr(token)})
    app = create_app(settings, llm_provider=SynchronizedCoordinator())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://acceptance"
        ) as client,
        asyncio.timeout(120),
    ):
        auth = {"Authorization": f"Bearer {token}"}
        payload = {
            "objective": "Validate seeded MNIST comparison; no external research.",
            "mode": "autonomous",
            "experiment_specification": {"epochs": 1, "train_samples": 128, "test_samples": 128},
        }
        first, second = await asyncio.gather(
            client.post("/agent/run", headers=auth, json={**payload, "budget": {"model_calls": 1}}),
            client.post("/agent/run", headers=auth, json=payload),
        )
        first.raise_for_status()
        second.raise_for_status()
        limited, normal = first.json(), second.json()
        assert limited["status"] == "budget_exceeded", limited["status"]
        assert normal["status"] == "succeeded", normal["status"]
        assert limited["metrics"]["model_calls"] == 1
        assert normal["metrics"]["model_calls"] == 3
        assert normal["metrics"]["training_runs"] == 1
        assert limited["task_id"] != normal["task_id"]
        seen: set[str] = set()
        verified = 0
        for task in (limited, normal):
            assert task["metrics"]["total_tokens"] == 0
            history = await client.get(
                f"/agent/tasks/{task['task_id']}/workflow/steps",
                headers=auth,
            )
            history.raise_for_status()
            for row in history.json()["items"]:
                child_id = row["child_task_id"]
                assert child_id not in seen
                seen.add(child_id)
                child = (await client.get(f"/agent/tasks/{child_id}", headers=auth)).json()
                assert child["parent_task_id"] == task["task_id"]
                assert child["status"] != "running"
                if child["mode"] == "experiment":
                    manifest = (
                        await client.get(
                            f"/agent/tasks/{child_id}/artifacts",
                            headers=auth,
                        )
                    ).json()
                    for item in manifest["items"]:
                        artifact = await client.get(
                            f"/agent/tasks/{child_id}/artifacts/{item['id']}",
                            headers=auth,
                        )
                        artifact.raise_for_status()
                        assert hashlib.sha256(artifact.content).hexdigest() == item["sha256"]
                        verified += 1
        assert verified == 10
        print(
            json.dumps(
                {
                    "acceptance": "passed",
                    "scenario": "concurrent-budget-isolation",
                    "limited_task": limited["task_id"],
                    "successful_task": normal["task_id"],
                    "verified_artifacts": verified,
                    "coordinator": "scripted-offline",
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
