"""Disjoint lanes of 15 scripted SDK/Docker paths; never a paid-model measurement."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

from autoscholar.core.config import Settings
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_contract import TASKS
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.paired_protocol import baseline_passed, history_snapshot
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.repair_validation import physical_accounting
from autoscholar.evaluation.repaired_paired import RepairedPairedLedger, repaired_paired_contract
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.compatibility import deepseek_schema
from autoscholar.llm.models import ChatMessage, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_live_ablation import SnapshotFixtureProvider
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


async def main(controller: str, history: Path, lane: int, lanes: int) -> None:
    if not 0 <= lane < lanes <= 3:
        raise ValueError("Expected disjoint lanes 0..lanes-1; at most three dedicated controllers")
    root = Path(__file__).resolve().parents[2]
    base = root / "data/evaluation"
    protected, _ = history_snapshot(history, base)
    source = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "src").rglob("*.py")}
    schedule = [task for i, task in enumerate(TASKS) if i % lanes == lane]
    output = root / "data/validation" / (f"free-v11-lane{lane}-" + uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    sandbox = DockerEvaluationSandbox(controller)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    assert resources == repaired_paired_contract()["resources"]
    assert await no_owned_children(sandbox)
    records = []
    try:
        for task in schedule:
            scenario, _, variant_name = task.partition("-")
            variant = next(value for value in WORKFLOW_VARIANTS if value == variant_name)
            data = fixture()
            script = data.scripts[scenario].model_copy(
                update={"prompt": data.scripts[scenario].prompt + SUFFIX}
            )
            cls = SnapshotFixtureProvider if variant == "no_planner" else RenamedFixtureProvider
            scripted = cls(data, script, project_source=PUBLIC_KNOWLEDGE)
            target = output / task
            target.mkdir()
            coding_seen: list[bool] = []

            async def respond(
                request: httpx.Request,
                _scripted: SnapshotFixtureProvider = scripted,
                _coding_seen: list[bool] = coding_seen,
            ) -> httpx.Response:
                body = json.loads(request.content)
                assert body["max_tokens"] == 8192 and len(request.content) <= 32768
                names = [tool["function"]["name"] for tool in body["tools"]]
                if "submit_code_ready" in names:
                    assert body["tool_choice"] == "required"
                    assert body["thinking"] == {"type": "disabled"}
                    _coding_seen.append(True)
                for tool in body["tools"]:
                    function = tool["function"]
                    if function["strict"]:
                        assert function["parameters"] == deepseek_schema(function["parameters"])
                result = await _scripted.generate(
                    [ChatMessage(**m) for m in body["messages"]],
                    tools=[ToolDefinition(**t["function"]) for t in body["tools"]],
                    tool_choice=body["tool_choice"],
                )
                call = result.tool_calls[0]
                return httpx.Response(
                    200,
                    json={
                        "id": "scripted-only",
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

            ledger = RepairedPairedLedger(target / "mock-ledger.json", run_id="script-only")
            transport = GuardedTransport(
                provider="llm",
                endpoint="https://api.deepseek.com/v1/chat/completions",
                ledger=ledger,
                output=target,
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
            async with httpx.AsyncClient(transport=transport) as client:
                native = OpenAICompatibleProvider(
                    settings, http_client=client, request_validator=transport.validate_payload
                )
                provider = RepairWorkflowProvider(native, transport, task)
                provider.verify_source = lambda: history_snapshot(history, base)
                row = await execute_task(
                    directory=target,
                    fixture=data,
                    script=script,
                    variant=variant,
                    provider=provider,
                    sandbox=sandbox,
                    resources=resources,
                    protocol_version=11,
                )
                row.update(
                    task=task,
                    scripted_model=True,
                    actual_vendor_requests=0,
                    mock_http_requests=len(ledger.attempts),
                )
                records.append(row)
                _write_json(output / "results.json", records)
                assert row["status"] == "measured" and row["intervention_verified"]
                assert baseline_passed(row) and row["checks"]["code_validated"]
                assert row["task_success"] is (
                    scenario == "normal" or (scenario == "recover" and variant != "no_replanning")
                )
                assert physical_accounting(row, provider) and not provider.failed
                assert 0 < len(ledger.attempts) <= 12 and coding_seen
                if scenario == "deception":
                    assert row["verification"] is not None
                    assert row["checks"]["independent_checkpoint_oracle"] is False
                if task == "recover-no_replanning":
                    assert row["error_code"] == "evaluation_replanning_disabled"
                    assert row["fault_mutations"] == row["usage"]["training_runs"] == 1
                assert await no_owned_children(sandbox)
            print(
                f"Lane {lane}: {task}; expected outcome; mock requests={len(ledger.attempts)}",
                flush=True,
            )
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
                "lane": lane,
                "lanes": lanes,
                "expected_tasks": schedule,
                "all_lane_paths_complete": [r["task"] for r in records] == schedule,
                "owned_children_cleaned": clean,
                "history_unchanged": unchanged,
                "source_unchanged": source_unchanged,
                "source_sha256": {p.relative_to(root).as_posix(): h for p, h in source.items()},
                "new_vendor_requests": 0,
                "real_model_measurement": False,
            },
        )
        print(f"Free v11 lane evidence: {output}; clean={clean}", flush=True)
        assert clean and unchanged and source_unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--history-index", type=Path, required=True)
    parser.add_argument("--lane", type=int, default=0)
    parser.add_argument("--lanes", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.history_index, args.lane, args.lanes))
