"""Replay one saved schema rejection through real Docker checks/training; no vendor API."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.evaluation.datasets import decode_json, read_bounded
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.paired_protocol import history_snapshot
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.repair_validation import physical_accounting
from autoscholar.evaluation.repaired_paired import RepairedPairedLedger, repaired_paired_contract
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.models import ChatMessage, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_coding_schema_feedback import completion
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


async def main(controller: str, history: Path, response: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    base = root / "data/evaluation"
    response = response.resolve()
    if not response.is_relative_to(base):
        raise ValueError("Saved response must stay under local evaluation evidence")
    raw = read_bounded(response)
    saved = decode_json(raw)
    assert isinstance(saved, dict)
    call = saved["choices"][0]["message"]["tool_calls"][0]["function"]
    assert call["name"] == "submit_code_ready"
    assert len(json.loads(call["arguments"])["summary"]) == 336
    protected, _ = history_snapshot(history, base)
    source = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "src").rglob("*.py")}
    output = root / "data/validation" / ("schema-feedback-docker-" + uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    sandbox = DockerEvaluationSandbox(controller)
    data = fixture()
    script = data.scripts["normal"].model_copy(
        update={"prompt": data.scripts["normal"].prompt + SUFFIX}
    )
    scripted = RenamedFixtureProvider(data, script, project_source=PUBLIC_KNOWLEDGE)
    coding_requests = 0
    row = {}
    try:
        resources = sandbox_resources(await sandbox.health(), dataset=True)
        assert resources == repaired_paired_contract()["resources"]
        assert await no_owned_children(sandbox)

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal coding_requests
            body = json.loads(request.content)
            assert len(request.content) <= 32768 and body["max_tokens"] == 8192
            names = [tool["function"]["name"] for tool in body["tools"]]
            if "submit_code_ready" in names:
                coding_requests += 1
                assert body["thinking"] == {"type": "disabled"}
                context = json.loads(body["messages"][-1]["content"])
                if coding_requests == 1:
                    assert context["protocol_feedback"] is None
                    return httpx.Response(
                        200, content=raw, headers={"content-type": "application/json"}
                    )
                assert coding_requests == 2 and "local tool schema" in context["protocol_feedback"]
                assert context["output_contract"]["field_character_limits"][
                    "submit_code_ready"
                ] == {"summary": 240}
            result = await scripted.generate(
                [ChatMessage(**m) for m in body["messages"]],
                tools=[ToolDefinition(**t["function"]) for t in body["tools"]],
                tool_choice=body["tool_choice"],
            )
            payload = completion([(c.name, c.arguments) for c in result.tool_calls])
            # All other decisions are explicit fixtures; matching the saved identifier is
            # solely for exercising the identity gate, never a claim of real model behavior.
            payload["model"] = saved["model"]
            return httpx.Response(200, json=payload)

        ledger = RepairedPairedLedger(
            output / "mock-ledger.json", run_id="offline-only", protocol_version=12
        )
        transport = GuardedTransport(
            provider="llm",
            endpoint="https://offline.invalid/v1/chat/completions",
            ledger=ledger,
            output=output,
            secrets=("offline-placeholder",),
            max_output_tokens=8192,
            delegate=httpx.MockTransport(respond),
        )
        config = Settings(
            _env_file=None,  # type: ignore[call-arg]
            llm_base_url="https://offline.invalid/v1",
            llm_model="offline-replay",
            llm_compatibility_profile="deepseek",
            llm_api_key=SecretStr("offline-placeholder"),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            native = OpenAICompatibleProvider(
                config, http_client=client, request_validator=transport.validate_payload
            )
            provider = RepairWorkflowProvider(native, transport, "normal-no_reviewer")
            provider.verify_source = lambda: history_snapshot(history, base)
            row = await execute_task(
                directory=output,
                fixture=data,
                script=script,
                variant="no_reviewer",
                provider=provider,
                sandbox=sandbox,
                resources=resources,
                protocol_version=12,
            )
            row.update(
                task="normal-no_reviewer",
                scripted_model=True,
                new_vendor_requests=0,
                real_model_measurement=False,
                mock_http_requests=len(ledger.attempts),
                schema_corrections=provider.corrections,
                original_response_sha256=hashlib.sha256(raw).hexdigest(),
            )
            _write_json(output / "result.json", row)
            assert row["status"] == "measured" and row["task_success"] is True, row
            assert all(row["checks"].values()) and row["intervention_verified"]
            assert row["step_counts"] == {"research": 1, "code": 1, "train": 1}
            assert row["paths"]["stage_calls"]["coding"] == 2
            assert not row["paths"]["stage_calls"].get("submit_review")
            assert row["verification"]["verified_artifact_count"] == 10
            assert physical_accounting(row, provider) and not provider.failed
            assert coding_requests == 2 and provider.corrections == 1
            assert 0 < len(ledger.attempts) <= 12
            _write_json(output / "request_ledger.json", ledger.data)
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        unchanged = history_snapshot(history, base)[0] == protected and response.read_bytes() == raw
        source_unchanged = all(
            hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in source.items()
        )
        _write_json(
            output / "cleanup.json",
            {
                "owned_children_cleaned": clean,
                "history_unchanged": unchanged,
                "source_unchanged": source_unchanged,
                "new_vendor_requests": 0,
                "real_model_measurement": False,
                "workflow_passed": row.get("task_success") is True,
                "source_sha256": {p.relative_to(root).as_posix(): h for p, h in source.items()},
            },
        )
        print(f"Free schema-feedback evidence: {output}; clean={clean}", flush=True)
        assert clean and unchanged and source_unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--history-index", type=Path, required=True)
    parser.add_argument("--saved-response", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.history_index, args.saved_response))
