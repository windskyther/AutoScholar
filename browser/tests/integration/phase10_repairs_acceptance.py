"""Explicit scripted LLM with real Docker training/oracle; no external API requests."""

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_v2 import SUFFIX, plan_roles
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import (
    REPAIR_TASKS,
    RepairLedger,
    RepairWorkflowProvider,
)
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.orchestration.models import TaskPlan
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


class UnnecessaryCodingRetryProvider(RenamedFixtureProvider):
    invalid_revision_sent = False
    recorded_review: dict[str, Any] | None = None
    recorded_plan: TaskPlan | None = None
    recorded_ids: dict[str, str] | None = None
    review_replayed = False
    review_corrections = 0

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        # The scripted fixture reads the JSON payload from the last user message;
        # a real bounded correction adds a separate, non-JSON user message.
        fixture_messages = messages
        if tools and tools[0].name == "submit_plan_revision" and len(messages) > 2:
            assert "Artifact-only recovery" in messages[-1].content
            fixture_messages = messages[:2]
        result = await super().generate(fixture_messages, tools=tools, tool_choice=tool_choice)
        call = result.tool_calls[0]
        if call.name == "submit_task_plan" and self.recorded_plan is not None:
            assert self.recorded_ids is not None
            arguments = self.recorded_plan.model_dump()
            for step in arguments["steps"]:
                step["id"] = self.recorded_ids[step["id"]]
                step["dependencies"] = [self.recorded_ids[key] for key in step["dependencies"]]
            return replace(result, tool_calls=(replace(call, arguments=arguments),))
        if call.name == "submit_review" and self.recorded_review and not self.review_replayed:
            self.review_replayed = True
            assert self.recorded_ids is not None
            arguments = json.loads(json.dumps(self.recorded_review))
            for issue in arguments["issues"]:
                issue["step_id"] = self.recorded_ids[issue["step_id"]]
            arguments["suggested_steps"] = [
                self.recorded_ids[key] for key in arguments["suggested_steps"]
            ]
            return replace(result, tool_calls=(replace(call, arguments=arguments),))
        if call.name == "submit_review" and len(messages) > 2:
            assert any(
                text in messages[-1].content
                for text in (
                    "contradicts verified facts",
                    "source defect requires source_path",
                    "not coding-stage deliverables",
                )
            )
            self.review_corrections += 1
            # Script an explicit failure grounded in current facts, never a forced PASS.
            payload = json.loads(messages[1].content)
            candidates = payload["collection_retry_candidates"]
            if candidates:
                return replace(
                    result,
                    tool_calls=(
                        replace(
                            call,
                            arguments={
                                "status": "REPLAN",
                                "issues": [
                                    {
                                        "step_id": key,
                                        "code": "collected_metrics_need_retry",
                                        "check": "raw_metrics",
                                        "message": "Collection failed after successful training",
                                        "requirement_id": "experiment.raw_metrics",
                                    }
                                    for key in candidates
                                ],
                                "suggested_steps": candidates,
                            },
                        ),
                    ),
                )
        if call.name == "submit_plan_revision" and not self.invalid_revision_sent:
            self.invalid_revision_sent = True
            return replace(
                result,
                tool_calls=(
                    replace(
                        call, arguments={**call.arguments, "rerun_steps": ["x-code", "x-train"]}
                    ),
                ),
            )
        return result


async def main(controller: str, replay_run: Path | None = None, protocol_version: int = 3) -> None:
    output = (
        Path(__file__).resolve().parents[2]
        / "data/validation"
        / ("repairs-preflight-" + uuid4().hex[:8])
    )
    output.mkdir(parents=True, exist_ok=False)
    sandbox = DockerEvaluationSandbox(controller)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    if not await no_owned_children(sandbox):
        raise ValueError("Dedicated controller already owns children")
    records = []
    replay_hashes: dict[Path, str] = {}
    if replay_run is not None:
        replay_run = replay_run.resolve()
        root = (Path(__file__).resolve().parents[2] / "data/evaluation").resolve()
        if not replay_run.is_relative_to(root):
            raise ValueError("Replay evidence must remain within ignored evaluation data")
        audits = sorted(replay_run.glob("audit-close-*.json"))
        if len(audits) != 1:
            raise ValueError("Replay requires exactly one immutable closing audit")
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        replay_hashes[audits[0]] = hashlib.sha256(audits[0].read_bytes()).hexdigest()
        for name, digest in audit["primary_sha256"].items():
            path = (root / name).resolve()
            assert (
                path.is_relative_to(root)
                and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            )
            replay_hashes[path] = digest
    try:
        for task in REPAIR_TASKS:
            scenario, _, variant = task.partition("-")
            data = fixture()
            script = data.scripts[scenario].model_copy(
                update={"prompt": data.scripts[scenario].prompt + SUFFIX}
            )
            target = output / task
            target.mkdir()
            ledger = RepairLedger(
                target / "fake-ledger.json",
                run_id="free-preflight",
                protocol_version=protocol_version,
            )
            transport = GuardedTransport(
                provider="llm",
                endpoint="https://example.org/chat/completions",
                ledger=ledger,
                output=target,
                secrets=(),
                delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
                max_output_tokens=8192,
            )
            native = UnnecessaryCodingRetryProvider(data, script, project_source=PUBLIC_KNOWLEDGE)
            if replay_run is not None:
                original = json.loads(
                    (replay_run / task / "workflow.json").read_text(encoding="utf-8")
                )
                initial = TaskPlan.model_validate(
                    min(original["plans"], key=lambda p: p["version"])["payload"]
                )
                roles = plan_roles(initial)
                assert roles is not None
                native.recorded_ids = {key: "x-" + role for role, key in roles.items()}
                native.recorded_plan = initial
                receipts = sorted(
                    replay_run.glob(f"response-llm-{task}--*.json"),
                    key=lambda path: int(path.stem.rsplit("--", 1)[1]),
                )
                for receipt_path in receipts:
                    assert receipt_path in replay_hashes
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    for old_call in receipt["choices"][0]["message"].get("tool_calls", []):
                        if old_call["function"]["name"] == "submit_review":
                            native.recorded_review = json.loads(old_call["function"]["arguments"])
                assert native.recorded_review is not None
            provider = RepairWorkflowProvider(native, transport, task)
            row = await execute_task(
                directory=target,
                fixture=data,
                script=script,
                variant="no_replanning" if variant == "no_replanning" else "baseline",
                provider=provider,
                sandbox=sandbox,
                resources=resources,
                protocol_version=protocol_version,
            )
            row.update(task=task, fixture_model=True, actual_vendor_requests=len(ledger.attempts))
            row.update(
                recorded_review_replayed=native.review_replayed,
                review_corrections=native.review_corrections,
            )
            records.append(row)
            _write_json(output / "preflight.json", records)
            expected = variant != "no_replanning"
            assert row["status"] == "measured" and row["task_success"] is expected, row
            assert row["intervention_verified"] and not ledger.attempts
            assert row["step_counts"]["research"] == row["step_counts"]["code"] == 1
            if replay_run is not None:
                assert native.review_replayed
                assert native.recorded_ids is not None and native.recorded_review is not None
                # Old and newer runs have different diagnoses. A real saved PASS needs
                # no correction; unsupported coding claims require one bounded correction.
                has_coding_issue = any(
                    native.recorded_ids[issue["step_id"]] == "x-code"
                    for issue in native.recorded_review["issues"]
                )
                assert native.review_corrections == int(
                    has_coding_issue
                    or any(
                        issue.get("check") == "verbatim_citation"
                        for issue in native.recorded_review["issues"]
                    )
                )
            if task == "recover-baseline":
                assert native.invalid_revision_sent
                assert provider.stage_calls["submit_plan_revision"] == 2
                assert row["usage"]["training_runs"] == 2 and row["fault_mutations"] == 1
            if task == "recover-no_replanning":
                assert row["error_code"] == "evaluation_replanning_disabled"
                assert row["usage"]["training_runs"] == 1 and row["fault_mutations"] == 1
            print(f"{task}: expected={expected}, actual={row['task_success']}", flush=True)
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        _write_json(
            output / "cleanup.json",
            {
                "owned_children_cleaned": clean,
                "resources": resources,
                "external_api_calls": 0,
            },
        )
        print(f"Free Docker repair preflight: {output}; clean={clean}", flush=True)
        assert clean
        assert all(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest
            for path, digest in replay_hashes.items()
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--protocol-version", type=int, choices=(3, 4, 5, 6), default=3)
    parser.add_argument(
        "--replay-run", type=Path, help="Read-only saved Reviewer responses; no APIs"
    )
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.replay_run, args.protocol_version))
