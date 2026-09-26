from pathlib import Path

import httpx
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.main import create_app
from autoscholar.orchestration.durable import DurableService
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import script, workflow
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_health import FakeDependency


async def test_durable_api_auth_idempotency_and_approval(tmp_path: Path) -> None:
    provider = ScriptedProvider(script())
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    app = create_app(
        Settings(experiment_api_token=SecretStr("phase7-token"), workflow_approval_threshold=0),
        database=FakeDependency(),
        redis=FakeDependency(),
        qdrant=FakeDependency(),
        llm_provider=provider,
        agent_repository=service.tasks,
        workspace_manager=service.workspace,
        sandbox_executor=FakeExperimentSandbox(),
    )
    app.state.durable_service = DurableService(service, approval_threshold=0)
    durable: DurableService = app.state.durable_service
    auth = {"Authorization": "Bearer phase7-token"}
    payload = {"objective": "比较 MNIST 模型", "mode": "autonomous"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            assert (
                await api.post("/agent/tasks", json=payload, headers={"Idempotency-Key": "api"})
            ).status_code == 401
            assert (await api.post("/agent/tasks", json=payload, headers=auth)).status_code == 422
            response = await api.post(
                "/agent/tasks", json=payload, headers=auth | {"Idempotency-Key": "api"}
            )
            assert response.status_code == 202, response.text
            task_id = response.json()["task_id"]
            repeated = await api.post(
                "/agent/tasks", json=payload, headers=auth | {"Idempotency-Key": "api"}
            )
            assert repeated.json()["task_id"] == task_id and not repeated.json()["created"]
            assert (await api.post("/agent/run", json=payload, headers=auth)).status_code == 409
            for _ in range(3):
                await durable.tick()
            paths = [
                f"/agent/tasks/{task_id}/execution",
                f"/agent/tasks/{task_id}/memory",
                f"/agent/tasks/{task_id}/approvals",
                f"/agent/tasks/{task_id}/durable/events",
                f"/agent/tasks/{task_id}/durable/checkpoints",
            ]
            for path in paths:
                assert (await api.get(path)).status_code == 401
                assert (await api.get(path, headers=auth)).status_code == 200
            approvals = (await api.get(paths[2], headers=auth)).json()["items"]
            approval = approvals[0]
            endpoint = f"/agent/tasks/{task_id}/approvals/{approval['id']}/decision"
            decision = {"action": "approve", "operation_sha256": approval["operation_sha256"]}
            assert (await api.post(endpoint, json=decision)).status_code == 401
            assert (await api.post(endpoint, json=decision, headers=auth)).json()[
                "status"
            ] == "paused"
            assert (
                await api.post(f"/agent/tasks/{task_id}/resume", headers=auth)
            ).status_code == 200
            for _ in range(3):
                await durable.tick()
            detail = (await api.get(f"/agent/tasks/{task_id}", headers=auth)).json()
            assert detail["status"] == "succeeded"
            assert detail["objective"] == payload["objective"]
            assert detail["metrics"]["artifact_count"] == 10
            for path in ("/projects/missing/memory", "/projects/missing/experiences"):
                assert (await api.get(path)).status_code == 401
                assert (await api.get(path, headers=auth)).status_code == 404
    finally:
        await engine.dispose()
