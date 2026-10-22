"""Explicit scripted LLM with real Docker training/oracle; no external API requests."""

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx

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
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    ToolChoice,
    ToolDefinition,
)
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


class UnnecessaryCodingRetryProvider(RenamedFixtureProvider):
    invalid_revision_sent = False

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


async def main(controller: str) -> None:
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
    try:
        for task in REPAIR_TASKS:
            scenario, _, variant = task.partition("-")
            data = fixture()
            script = data.scripts[scenario].model_copy(
                update={"prompt": data.scripts[scenario].prompt + SUFFIX}
            )
            target = output / task
            target.mkdir()
            ledger = RepairLedger(target / "fake-ledger.json", run_id="free-preflight")
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
            provider = RepairWorkflowProvider(native, transport, task)
            row = await execute_task(
                directory=target,
                fixture=data,
                script=script,
                variant="no_replanning" if variant == "no_replanning" else "baseline",
                provider=provider,
                sandbox=sandbox,
                resources=resources,
                protocol_version=3,
            )
            row.update(task=task, fixture_model=True, actual_vendor_requests=len(ledger.attempts))
            records.append(row)
            _write_json(output / "preflight.json", records)
            expected = variant != "no_replanning"
            assert row["status"] == "measured" and row["task_success"] is expected, row
            assert row["intervention_verified"] and not ledger.attempts
            assert row["step_counts"]["research"] == row["step_counts"]["code"] == 1
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    asyncio.run(main(parser.parse_args().sandbox_container))
