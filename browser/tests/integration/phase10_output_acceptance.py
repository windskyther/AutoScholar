"""Saved truncated response + bounded scripted edits + real Docker; zero vendor requests."""

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

from autoscholar.coding.agent import CodingAgent, CodingLimits
from autoscholar.coding.snapshot import snapshot_violation
from autoscholar.coding.workspace import WorkspaceManager
from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.core.config import Settings
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import ObservedSandbox, sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.paired_protocol import history_snapshot
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import (
    REPAIR_TASKS,
    RepairLedger,
    RepairWorkflowProvider,
)
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from autoscholar.llm.errors import LLMResponseError
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_coding_agent import repository
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


class LocalEdits(RenamedFixtureProvider):
    coding_calls = 0
    invalid_source = False

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
        if result.tool_calls[0].name != "submit_code_ready":
            return result
        self.coding_calls += 1
        if self.invalid_source and self.coding_calls > 1:
            return result
        payload = json.loads(messages[-1].content)
        assert tool_choice == "required"
        if self.invalid_source:
            call = replace(
                result.tool_calls[0],
                name="create_file",
                arguments={"path": "unused_checker.py", "content": "import math\n"},
            )
        else:
            source = payload["source_files"]["train.py"]
            anchor = source.rstrip("\n").splitlines()[-1] + "\n"
            call = replace(
                result.tool_calls[0],
                name="edit_file",
                arguments={
                    "path": "train.py",
                    "old_text": anchor,
                    "new_text": anchor + f"\n# local scripted edit {self.coding_calls}\n",
                },
            )
        assert snapshot_violation(call) is None
        return replace(result, tool_calls=(call,))


async def replay_cut(
    root: Path,
    output: Path,
    sandbox: DockerEvaluationSandbox,
    resources: dict[str, str],
    stopped: Path,
) -> None:
    old_request = json.loads(
        (stopped / "request-llm-normal-baseline--7.json").read_text(encoding="utf-8")
    )
    old_response = json.loads(
        (stopped / "response-llm-normal-baseline--7.json").read_text(encoding="utf-8")
    )
    payload = json.loads(old_request["messages"][-1]["content"])
    sent = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=old_response)

    store, engine = await repository()
    task = "replayed-cut"
    await store.create_task(task_id=task, objective=payload["objective"])
    workspace = WorkspaceManager(output / "replayed-cut")
    workspace.initialize(task)
    for name, content in payload["source_files"].items():
        workspace.write_text(task, name, content)
    observed = ObservedSandbox(sandbox, resources)
    budget = Budget(BudgetLimits(model_calls=12, total_tokens=120000))
    token = current_budget.set(budget)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            settings = Settings(
                _env_file=None,  # type: ignore[call-arg]  # BaseSettings option, not a schema field.
                llm_api_key=SecretStr("offline-replay-fixture"),
                llm_base_url="https://example.org/v1",
                llm_model="offline-replay",
            )
            provider = OpenAICompatibleProvider(settings, http_client=client)
            agent = CodingAgent(
                provider=BudgetedLLM(provider),
                repository=store,
                workspaces=workspace,
                sandbox=observed,
                limits=CodingLimits(max_repairs=0),
            )
            try:
                await agent.run(
                    task_id=task, objective=payload["objective"], plan=[], snapshot_mode=True
                )
            except LLMResponseError as exc:
                assert exc.code == "llm_response_truncated" and exc.finish_reason == "length"
                assert not exc.retryable and exc.usage and exc.usage.total_tokens == 13827
            else:
                raise AssertionError("Saved truncated response was accepted")
        assert len(sent) == 1 and not observed.runs
        assert workspace.source_snapshot(task) == payload["source_files"]
        child = await store.get_task(task)
        assert child is not None and not child.tool_calls
        _write_json(
            output / "saved-cut-replay.json",
            {
                "vendor_requests": 0,
                "mock_http_requests": 1,
                "saved_response_unchanged": True,
                "error": "llm_response_truncated",
                "writes": 0,
                "sandbox_runs": 0,
                "replayed_token_receipt": budget.used,
                "source_preserved": True,
            },
        )
        print(
            "Saved truncated v8 response: whole batch rejected, zero writes/runs/retries",
            flush=True,
        )
    finally:
        current_budget.reset(token)
        await engine.dispose()


async def main(controller: str, history: Path, stopped: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    base = root / "data/evaluation"
    protected, _ = history_snapshot(history, base)
    stopped = stopped.resolve()
    assert stopped.is_relative_to(base.resolve())
    fingerprints = {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "src").rglob("*.py")
    }
    output = root / "data/validation" / ("output-preflight-" + uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    sandbox = DockerEvaluationSandbox(controller)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    assert await no_owned_children(sandbox)
    records = []
    try:
        await replay_cut(root, output, sandbox, resources, stopped)
        for task in ("invalid-local-source", *REPAIR_TASKS):
            scenario, _, variant = task.partition("-")
            invalid = task == "invalid-local-source"
            if invalid:
                scenario = "normal"
            data = fixture()
            script = data.scripts[scenario].model_copy(
                update={"prompt": data.scripts[scenario].prompt + SUFFIX}
            )
            target = output / task
            target.mkdir()
            ledger = RepairLedger(
                target / "fake-ledger.json", run_id="free-output-repair", protocol_version=6
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
            native = LocalEdits(data, script, project_source=PUBLIC_KNOWLEDGE)
            native.invalid_source = invalid
            provider = RepairWorkflowProvider(
                native, transport, "normal-baseline" if invalid else task
            )
            provider.verify_source = lambda: history_snapshot(history, base)
            row = await execute_task(
                directory=target,
                fixture=data,
                script=script,
                variant="no_replanning" if variant == "no_replanning" else "baseline",
                provider=provider,
                sandbox=sandbox,
                resources=resources,
                protocol_version=8,
            )
            row.update(
                task=task,
                scripted_model=True,
                actual_vendor_requests=len(ledger.attempts),
                local_edit_calls=native.coding_calls,
            )
            records.append(row)
            _write_json(output / "results.json", records)
            assert not ledger.attempts and provider.calls <= 12
            if invalid:
                assert row["task_success"] is False and not row["checks"]["code_validated"]
                assert row["usage"].get("training_runs", 0) == 0 and row["fault_mutations"] == 0
            else:
                assert row["task_success"] is (variant != "no_replanning"), row
                assert native.coding_calls == 4 and row["checks"]["code_validated"]
                assert row["usage"]["training_runs"] == (2 if task == "recover-baseline" else 1)
                if variant == "no_replanning":
                    assert row["error_code"] == "evaluation_replanning_disabled"
            assert await no_owned_children(sandbox)
            print(f"{task}: expected outcome; local edits={native.coding_calls}", flush=True)
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        unchanged = all(
            hashlib.sha256((base / name).read_bytes()).hexdigest() == digest
            for name, digest in protected.items()
        )
        source_unchanged = all(
            hashlib.sha256(p.read_bytes()).hexdigest() == digest
            for p, digest in fingerprints.items()
        )
        _write_json(
            output / "cleanup.json",
            {
                "owned_children_cleaned": clean,
                "history_unchanged": unchanged,
                "source_unchanged": source_unchanged,
                "external_api_calls": 0,
                "all_cases_complete": len(records) == 4,
            },
        )
        print(
            f"Free output-repair evidence: {output}; clean={clean}; unchanged={unchanged}",
            flush=True,
        )
        assert clean and unchanged and source_unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--history-index", type=Path, required=True)
    parser.add_argument("--stopped-run", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.history_index, args.stopped_run))
