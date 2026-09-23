"""Real API + configured LLM + Docker acceptance; optional test-only metric fault."""

import argparse
import asyncio
import hashlib
import json
import secrets
from typing import Any

import httpx
from pydantic import SecretStr

from autoscholar.coding.sandbox import (
    SandboxClient,
    SandboxExecutor,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.core.config import Settings
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.main import create_app


class OfflineCoordinator:
    """Scripted test double, NOT evidence that a real model can plan or review."""

    configured = True

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        del tool_choice
        structured = {"submit_task_plan", "submit_review", "submit_plan_revision"}
        name = next(
            (tool.name for tool in (tools or []) if tool.name in structured),
            "submit_code_ready",
        )
        payload = json.loads(messages[-1].content) if name in structured else {}
        args: dict[str, Any]
        if name == "submit_task_plan":
            args = {
                "goal": payload["objective"],
                "steps": [
                    {
                        "id": "code",
                        "type": "coding",
                        "description": "Validate seeded code",
                        "expected_output": "Validated train.py and test_models.py",
                    },
                    {
                        "id": "train",
                        "type": "experiment",
                        "description": "Run real MNIST comparison",
                        "expected_output": "Measured metrics and artifacts",
                        "dependencies": ["code"],
                    },
                ],
            }
        elif name == "submit_review":
            # The real deterministic reviewer must reject this optimistic test verdict
            # when the intentionally damaged experiment has failed.
            args = {"status": "PASS"}
        elif name == "submit_plan_revision":
            args = {
                "plan": payload["plan"],
                "rerun_steps": ["train"],
                "reason": "Retry test-injected missing artifact",
            }
        else:
            name = "submit_code_ready"
            args = {"summary": "Validate seeded sources with real static checks and pytest"}
        return LLMResult(
            text="",
            model="offline-scripted-coordinator",
            usage=TokenUsage(0, 0, 0),
            tool_calls=(ToolCall(id=secrets.token_hex(8), name=name, arguments=args),),
        )

    async def close(self) -> None:
        return None


class FaultOnceSandbox:
    """Never installed in the server; used only by this explicit acceptance command."""

    def __init__(self, inner: SandboxExecutor) -> None:
        self.inner = inner
        self.injected = False

    async def health(self) -> SandboxHealth:
        return await self.inner.health()

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        result = await self.inner.run(request)
        if request.collect_artifacts and result.status == "succeeded" and not self.injected:
            self.injected = True
            # Keep actual execution real; simulate loss of the required metric output.
            # No fake measurement or fake successful review is ever created.
            return result.model_copy(
                update={
                    "artifacts": [
                        a for a in result.artifacts if a.path != "outputs/raw_metrics.json"
                    ],
                }
            )
        return result

    async def close(self) -> None:
        await self.inner.close()


async def run_smoke(*, inject_fault: bool, research: bool, offline: bool = False) -> None:
    if offline and research:
        raise ValueError("--offline cannot be combined with external --research")
    settings = Settings()
    token = secrets.token_urlsafe(32)
    settings = settings.model_copy(update={"experiment_api_token": SecretStr(token)})
    client = SandboxClient(
        settings.sandbox_manager_url,
        timeout_seconds=settings.experiment_timeout_seconds + 10,
    )
    fault = FaultOnceSandbox(client) if inject_fault else None
    app = create_app(
        settings,
        sandbox_executor=fault or client,
        llm_provider=OfflineCoordinator() if offline else None,
    )
    objective = (
        "Compare MLP and CNN on the supplied small fixed MNIST subset using the working "
        "seeded templates. Inspect the source and run validation, then run the experiment "
        "and report measured results without assuming CNN must win. This is engineering "
        "acceptance, not a benchmark. Preserve the config and output contracts. "
    )
    objective += (
        "Use exactly three steps: one research, one coding (including validation), and one "
        "experiment. First use three focused web queries about the architectural "
        "distinction between MLP and CNN, and pass the evidence to coding. Do not conduct "
        "an extended survey, historical review, or search for benchmark accuracies."
        if research
        else "No external research is needed; use exactly one coding step and one experiment step."
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://acceptance",
            timeout=settings.autonomous_budget.wall_seconds + 30,
        ) as api,
    ):
        auth = {"Authorization": f"Bearer {token}"}
        payload = {
            "objective": objective,
            "mode": "autonomous",
            "research_sources": ["web"],
            "experiment_specification": {"epochs": 1, "train_samples": 128, "test_samples": 128},
        }
        assert (await api.post("/agent/run", json=payload)).status_code == 401
        response = await api.post("/agent/run", json=payload, headers=auth)
        response.raise_for_status()
        task = response.json()
        task_id = task["task_id"]
        detail = (await api.get(f"/agent/tasks/{task_id}", headers=auth)).json()
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": task["status"],
                    "metrics": task["metrics"],
                    "error_code": detail.get("error_code"),
                    "error_message": detail.get("error_message"),
                    "fault_injected": bool(fault and fault.injected),
                    "coordinator": "scripted-offline" if offline else "configured-llm",
                }
            ),
            flush=True,
        )
        assert task["status"] == "succeeded", "Workflow failed; inspect persisted histories"
        histories = {}
        for kind in ("plans", "steps", "reviews"):
            path = f"/agent/tasks/{task_id}/workflow/{kind}"
            assert (await api.get(path)).status_code == 401
            result = await api.get(path, headers=auth)
            result.raise_for_status()
            histories[kind] = result.json()["items"]
        latest = max(histories["reviews"], key=lambda item: item["plan_version"])
        assert latest["payload"]["status"] == "PASS"
        assert len({row["child_task_id"] for row in histories["steps"]}) == len(histories["steps"])
        web_evidence_count = 0
        if research:
            for row in histories["steps"]:
                child = await api.get(
                    f"/agent/tasks/{row['child_task_id']}",
                    headers=auth,
                )
                child.raise_for_status()
                if child.json()["mode"] == "research" and row["status"] == "succeeded":
                    web_evidence_count += sum(
                        evidence["provider"] == "tavily" and evidence["source_type"] == "web"
                        for evidence in child.json()["evidence"]
                    )
            assert web_evidence_count > 0, "Research acceptance requires real Tavily evidence"
        if inject_fault:
            assert fault and fault.injected
            assert task["metrics"]["replans"] >= 1
            assert any(row["payload"]["status"] == "REPLAN" for row in histories["reviews"])
            assert any(
                row["result"].get("error_code") == "experiment_artifacts_missing"
                for row in histories["steps"]
            )
        verified = 0
        experiments = []
        for step in histories["steps"]:
            output = step["result"]
            if "experiment_id" not in output or step["status"] != "succeeded":
                continue
            child_id = step["child_task_id"]
            assert output["source_sha256"] == output["expected_source_sha256"]
            assert (await api.get(f"/agent/tasks/{child_id}")).status_code == 401
            manifest = await api.get(f"/agent/tasks/{child_id}/artifacts", headers=auth)
            manifest.raise_for_status()
            for artifact in manifest.json()["items"]:
                download = await api.get(
                    f"/agent/tasks/{child_id}/artifacts/{artifact['id']}",
                    headers=auth,
                )
                download.raise_for_status()
                assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]
                verified += 1
            experiments.append(
                {
                    "child_task_id": child_id,
                    "experiment_id": output["experiment_id"],
                    "test_accuracy": {
                        run["model"]: run["test_accuracy"] for run in output["metrics"]["runs"]
                    },
                }
            )
        assert verified >= 10
        print(
            json.dumps(
                {
                    "acceptance": "passed",
                    "plan_versions": len(histories["plans"]),
                    "reviews": len(histories["reviews"]),
                    "verified_artifacts": verified,
                    "web_evidence_count": web_evidence_count,
                    "experiments": experiments,
                }
            ),
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inject-invalid-metrics", action="store_true")
    parser.add_argument("--research", action="store_true")
    parser.add_argument(
        "--offline", action="store_true", help="Scripted coordinator; no external API"
    )
    args = parser.parse_args()
    asyncio.run(
        run_smoke(
            inject_fault=args.inject_invalid_metrics,
            research=args.research,
            offline=args.offline,
        )
    )


if __name__ == "__main__":
    main()
