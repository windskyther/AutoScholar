"""Saved v6 edits, explicit scripted decisions, real Docker validation/training; no APIs."""

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import (
    REPAIR_TASKS,
    RepairLedger,
    RepairWorkflowProvider,
)
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


class SavedEditsProvider(RenamedFixtureProvider):
    """Replay raw public edits only. All subsequent review/replan decisions are scripts."""

    recorded_edits: list[dict[str, Any]]
    coding_calls = 0
    replay_saved = True

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
        if tools and len(tools) > 1 and any(t.name == "submit_code_ready" for t in tools):
            if self.replay_saved:
                saved = self.recorded_edits[self.coding_calls]
            else:
                payload = json.loads(messages[-1].content)
                source = payload["source_files"]["train.py"]
                saved = {
                    "name": "edit_file",
                    "arguments": {
                        "path": "train.py",
                        "old_text": source,
                        "new_text": source + f"\n# bounded editing turn {self.coding_calls + 1}\n",
                    },
                }
            self.coding_calls += 1
            return replace(
                result,
                tool_calls=(
                    replace(result.tool_calls[0], name=saved["name"], arguments=saved["arguments"]),
                ),
            )
        return result


async def handoff_reason(target: Path, valid: bool = True) -> str:
    saved = json.loads((target / "workflow.json").read_text(encoding="utf-8"))
    code = next(s for s in saved["steps"] if s["step_id"] == "x-code")
    database = target / saved["database"]
    engine = create_async_engine(
        URL.create(
            "sqlite+aiosqlite", database=database.as_uri(), query={"mode": "ro", "uri": "true"}
        )
    )
    try:
        store = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
        child = await store.get_task(code["child_task_id"])
        assert child is not None
        checks = [t for t in child.tool_calls if t.tool_name in {"static_check", "run_pytest"}]
        if valid:
            assert len(checks) == 2 and all(t.status == "succeeded" for t in checks)
        else:
            assert len(checks) == 1 and checks[0].tool_name == "static_check"
            assert checks[0].status == "failed" and "F841" in checks[0].output
        reasons = {t.arguments["handoff_reason"] for t in checks}
        assert reasons == {"model_call_reserve"}
        assert not any(t.tool_name == "submit_code_ready" for t in child.tool_calls)
        return "model_call_reserve"
    finally:
        await engine.dispose()


async def main(controller: str, replay_run: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    evidence_root = (root / "data/evaluation").resolve()
    replay_run = replay_run.resolve()
    if not replay_run.is_relative_to(evidence_root):
        raise ValueError("Replay must stay within ignored evaluation data")
    audit = json.loads((replay_run / "audit-close-20261027.json").read_text(encoding="utf-8"))
    protected: dict[Path, str] = {}
    for name, digest in audit["primary_sha256"].items():
        path = (evidence_root / name).resolve()
        assert path.is_relative_to(evidence_root)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        protected[path] = digest
    for path in replay_run.rglob("*"):
        if path.is_file():
            protected.setdefault(path, hashlib.sha256(path.read_bytes()).hexdigest())
    edits = []
    for ordinal in range(1, 13):
        receipt = json.loads(
            (replay_run / f"response-llm-recover-no_replanning--{ordinal}.json").read_text(
                encoding="utf-8"
            )
        )
        for call in receipt["choices"][0]["message"].get("tool_calls", []):
            if call["function"]["name"] == "edit_file":
                edits.append(
                    {"name": "edit_file", "arguments": json.loads(call["function"]["arguments"])}
                )
    assert len(edits) == 8
    output = root / "data/validation" / ("convergence-preflight-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    sandbox = DockerEvaluationSandbox(controller)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    assert await no_owned_children(sandbox)
    records = []
    try:
        cases = [("saved-edit-prefix", "normal-baseline", True)] + [
            (task, task, False) for task in REPAIR_TASKS
        ]
        for label, task, saved_replay in cases:
            scenario, _, variant = task.partition("-")
            data = fixture()
            script = data.scripts[scenario].model_copy(
                update={"prompt": data.scripts[scenario].prompt + SUFFIX}
            )
            target = output / label
            target.mkdir()
            ledger = RepairLedger(
                target / "fake-ledger.json", run_id="free-convergence", protocol_version=6
            )
            transport = GuardedTransport(
                provider="llm",
                endpoint="https://example.org/chat/completions",
                ledger=ledger,
                output=target,
                secrets=(),
                max_output_tokens=8192,
                delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
            )
            native = SavedEditsProvider(data, script, project_source=PUBLIC_KNOWLEDGE)
            native.recorded_edits = edits
            native.replay_saved = saved_replay
            provider = RepairWorkflowProvider(native, transport, task)
            row = await execute_task(
                directory=target,
                fixture=data,
                script=script,
                variant="no_replanning" if variant == "no_replanning" else "baseline",
                provider=provider,
                sandbox=sandbox,
                resources=resources,
                protocol_version=6,
            )
            row.update(
                task=label,
                fixture_model=True,
                actual_vendor_requests=len(ledger.attempts),
                coding_edit_calls=native.coding_calls,
                saved_coding_responses_replayed=native.coding_calls if saved_replay else 0,
            )
            records.append(row)
            _write_json(output / "results.json", records)
            assert not ledger.attempts and native.coding_calls == 4
            assert row["status"] == "measured" and row["intervention_verified"]
            assert row["step_counts"]["code"] == row["step_counts"]["research"] == 1
            if saved_replay:
                assert row["task_success"] is False and not row["checks"]["code_validated"]
                assert row["usage"].get("training_runs", 0) == row["fault_mutations"] == 0
                row["automatic_validation_handoff"] = await handoff_reason(target, valid=False)
                row["expected_invalid_prefix_rejected"] = True
                _write_json(output / "results.json", records)
                print(
                    "Saved v6 edit prefix: F841 detected; no training or implicit PASS", flush=True
                )
                continue
            assert row["task_success"] is (variant != "no_replanning"), row
            assert row["usage"]["training_runs"] == (2 if task == "recover-baseline" else 1)
            assert row["fault_mutations"] == (0 if scenario == "normal" else 1)
            row["automatic_validation_handoff"] = await handoff_reason(target)
            if variant == "no_replanning":
                assert row["error_code"] == "evaluation_replanning_disabled"
            _write_json(output / "results.json", records)
            print(
                f"{task}: expected outcome verified; saved edits={native.coding_calls}", flush=True
            )
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        unchanged = all(
            hashlib.sha256(p.read_bytes()).hexdigest() == d for p, d in protected.items()
        )
        _write_json(
            output / "cleanup.json",
            {
                "owned_children_cleaned": clean,
                "replayed_evidence_unchanged": unchanged,
                "external_api_calls": 0,
            },
        )
        print(
            f"Free convergence evidence: {output}; clean={clean}; unchanged={unchanged}", flush=True
        )
        assert clean and unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--replay-run", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.replay_run))
