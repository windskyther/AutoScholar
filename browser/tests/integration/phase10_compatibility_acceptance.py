"""One scripted normal baseline through the real SDK and Docker; zero vendor requests."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.evaluation.baseline_protocol import BaselineLedger
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.paired_protocol import baseline_passed, history_snapshot
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.repair_validation import physical_accounting
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.compatibility import deepseek_schema
from autoscholar.llm.models import ChatMessage, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


async def main(controller: str, history: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    base = root / "data/evaluation"
    protected, _ = history_snapshot(history, base)
    source = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "src").rglob("*.py")}
    output = root / "data/validation" / ("free-compat-baseline-" + uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    data = fixture()
    script = data.scripts["normal"].model_copy(
        update={"prompt": data.scripts["normal"].prompt + SUFFIX}
    )
    scripted = RenamedFixtureProvider(data, script, project_source=PUBLIC_KNOWLEDGE)
    observed = []

    async def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 8192 and len(request.content) <= 32768
        names = [tool["function"]["name"] for tool in body["tools"]]
        if "submit_code_ready" in names:
            assert body["thinking"] == {"type": "disabled"}
            assert body["tool_choice"] == "required"
        for tool in body["tools"]:
            function = tool["function"]
            if function["strict"]:
                assert function["parameters"] == deepseek_schema(function["parameters"])
        observed.append({"tools": names, "thinking": body.get("thinking")})
        result = await scripted.generate(
            [ChatMessage(**message) for message in body["messages"]],
            tools=[ToolDefinition(**tool["function"]) for tool in body["tools"]],
            tool_choice=body["tool_choice"],
        )
        call = result.tool_calls[0]
        return httpx.Response(
            200,
            json={
                "id": "offline-script-only",
                "object": "chat.completion",
                "created": 1,
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": json.dumps(call.arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    ledger = BaselineLedger(output / "mock-ledger.json", run_id="script-only", protocol_version=10)
    transport = GuardedTransport(
        provider="llm",
        endpoint="https://api.deepseek.com/v1/chat/completions",
        ledger=ledger,
        output=output,
        secrets=("offline-fixture",),
        max_output_tokens=8192,
        delegate=httpx.MockTransport(respond),
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_base_url="https://api.deepseek.com/v1",
        llm_model="deepseek-v4-flash",
        llm_api_key=SecretStr("offline-fixture"),
    )
    sandbox = DockerEvaluationSandbox(controller)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    assert await no_owned_children(sandbox)
    complete = False
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            native = OpenAICompatibleProvider(
                settings, http_client=client, request_validator=transport.validate_payload
            )
            provider = RepairWorkflowProvider(native, transport, "normal-baseline")
            provider.verify_source = lambda: history_snapshot(history, base)
            target = output / "normal-baseline"
            target.mkdir()
            row = await execute_task(
                directory=target,
                fixture=data,
                script=script,
                variant="baseline",
                provider=provider,
                sandbox=sandbox,
                resources=resources,
                protocol_version=10,
            )
            row.update(task="normal-baseline", scripted_model=True, actual_vendor_requests=0)
            _write_json(output / "result.json", row)
            assert baseline_passed(row) and row["task_success"] is True
            assert all(row["checks"].values()) and row["intervention_verified"]
            assert physical_accounting(row, provider)
            assert 0 < len(ledger.attempts) <= 12 and not provider.failed
            assert any("submit_code_ready" in item["tools"] for item in observed)
            complete = True
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        unchanged = history_snapshot(history, base)[0] == protected
        source_unchanged = all(
            hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in source.items()
        )
        _write_json(
            output / "cleanup.json",
            {
                "owned_children_cleaned": clean,
                "history_unchanged": unchanged,
                "source_unchanged": source_unchanged,
                "scripted_baseline_complete": complete,
                "mock_http_requests": len(ledger.attempts),
                "new_vendor_requests": 0,
                "real_model_acceptance": False,
                "observed_wire": observed,
            },
        )
        print(
            f"Free compatibility baseline: {output}; complete={complete}; clean={clean}", flush=True
        )
        assert clean and unchanged and source_unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--history-index", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.history_index))
