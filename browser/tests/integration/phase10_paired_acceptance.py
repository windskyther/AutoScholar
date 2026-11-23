"""All 15 v8 paths: explicitly scripted models, actual isolated Docker, zero APIs."""

import argparse
import asyncio
import hashlib
from pathlib import Path
from uuid import uuid4

import httpx

from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_contract import TASKS
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.paired_protocol import PairedLedger, baseline_passed, history_snapshot
from autoscholar.evaluation.provider_transport import GuardedTransport
from autoscholar.evaluation.provider_validation import no_owned_children
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.runner import _write_json
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE
from tests.test_live_ablation import SnapshotFixtureProvider
from tests.test_live_ablation_v2 import RenamedFixtureProvider, fixture


async def main(controller: str, history: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    base = root / "data/evaluation"
    protected, _ = history_snapshot(history, base)
    source_paths = [
        *(root / "src").rglob("*.py"),
        root / "benchmarks/ablations/workflow_fixture_v2.json",
    ]
    source = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    output = root / "data/validation" / ("paired-v8-preflight-" + uuid4().hex[:8])
    output.mkdir(exist_ok=False)
    sandbox = DockerEvaluationSandbox(controller)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    assert await no_owned_children(sandbox)
    records = []
    try:
        for task in TASKS:
            scenario, _, variant_name = task.partition("-")
            variant = next(v for v in WORKFLOW_VARIANTS if v == variant_name)
            data = fixture()
            script = data.scripts[scenario].model_copy(
                update={"prompt": data.scripts[scenario].prompt + SUFFIX}
            )
            target = output / task
            target.mkdir()
            ledger = PairedLedger(target / "fake-ledger.json", run_id="free-script-only")
            transport = GuardedTransport(
                provider="llm",
                endpoint="https://example.org/chat/completions",
                ledger=ledger,
                output=target,
                secrets=(),
                max_output_tokens=8192,
                delegate=httpx.MockTransport(lambda request: httpx.Response(500)),
            )
            provider_type = (
                SnapshotFixtureProvider if variant == "no_planner" else RenamedFixtureProvider
            )
            native = provider_type(data, script, project_source=PUBLIC_KNOWLEDGE)
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
                protocol_version=8,
            )
            row.update(task=task, scripted_model=True, actual_vendor_requests=len(ledger.attempts))
            records.append(row)
            _write_json(output / "results.json", records)
            assert not ledger.attempts and provider.calls <= 12
            assert row["status"] == "measured" and row["intervention_verified"]
            assert baseline_passed(row), row
            assert row["task_success"] is (
                scenario == "normal" or (scenario == "recover" and variant != "no_replanning")
            ), row
            assert row["checks"]["code_validated"]
            if scenario == "deception":
                assert (
                    row["verification"] is not None
                    and not row["checks"]["independent_checkpoint_oracle"]
                )
            if task == "recover-no_replanning":
                assert row["error_code"] == "evaluation_replanning_disabled"
                assert row["usage"]["training_runs"] == row["fault_mutations"] == 1
            assert await no_owned_children(sandbox)
            print(f"{task}: expected outcome; scripted calls={provider.calls}", flush=True)
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        historical_unchanged = all(
            hashlib.sha256((base / name).read_bytes()).hexdigest() == digest
            for name, digest in protected.items()
        )
        source_unchanged = all(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest
            for path, digest in source.items()
        )
        complete = [row["task"] for row in records] == list(TASKS)
        _write_json(
            output / "cleanup.json",
            {
                "owned_children_cleaned": clean,
                "historical_evidence_unchanged": historical_unchanged,
                "source_unchanged": source_unchanged,
                "external_api_calls": 0,
                "all_15_scripted_paths_complete": complete,
                "real_model_measurement": False,
            },
        )
        print(f"Free v8 evidence: {output}; complete={complete}; clean={clean}", flush=True)
        assert clean and historical_unchanged and source_unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-container", required=True)
    parser.add_argument("--history-index", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.sandbox_container, args.history_index))
