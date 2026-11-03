"""Prepare a reviewable free plan, then run it only after separate paid approval."""

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import payload_digest, read_bounded
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task, pilot_summary
from autoscholar.evaluation.live_ablation_v2 import OUTPUT_TOKENS, SUFFIX
from autoscholar.evaluation.provider_prior import read_public
from autoscholar.evaluation.provider_transport import GuardedTransport, approval_path
from autoscholar.evaluation.provider_validation import load_explicit_settings, no_owned_children
from autoscholar.evaluation.repair_protocol import (
    RepairLedger,
    RepairWorkflowProvider,
    repair_claim,
    repair_contract,
)
from autoscholar.evaluation.runner import _git_state, _write_json
from autoscholar.evaluation.workflow_ablation import WorkflowVariant
from autoscholar.evaluation.workflow_fixture import WorkflowFixture
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider

_RUNNER_PATHS = {
    "src/autoscholar/evaluation/repair_validation.py",
    "src/autoscholar/evaluation/repair_protocol.py",
    "src/autoscholar/evaluation/__main__.py",
    "src/autoscholar/evaluation/provider_transport.py",
}


def runner_only_changes(repo_root: Path, revision: str) -> bool:
    """Approval permits a new runner, not an unreviewed change to the Agent under test."""
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        return False
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if ancestor.returncode != 0:
        return False
    changed = subprocess.run(
        ["git", "diff", "--name-only", revision, "HEAD", "--", "src"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.splitlines()
    return set(changed) <= _RUNNER_PATHS


def approved_proposal(path: Path, repo_root: Path, protocol_version: int = 4) -> dict[str, Any]:
    root = (repo_root / "data/evaluation").resolve()
    proposal, digest = read_public(path.resolve(), root)
    expected = repair_contract(protocol_version)
    expected["scope"] += "_proposal"
    if (
        not isinstance(proposal, dict)
        or proposal.get("schema_version") != 1
        or proposal.get("contract") != expected
        or proposal.get("paid_execution_approved") is not False
        or proposal.get("paid_execution_available") is not False
        or proposal.get("external_api_calls") != 0
        or proposal.get("source_ready") is not True
        or not isinstance(proposal.get("git_revision"), str)
        or not runner_only_changes(repo_root, proposal["git_revision"])
    ):
        raise ValueError(
            "Approved proposal scope/runtime changed; separate scope approval required"
        )
    protected = proposal.get("protected_previous_records_sha256")
    if not isinstance(protected, dict) or not protected:
        raise ValueError("Approved proposal must protect the old grant and claim")
    if protocol_version >= 5:
        required = {repair_claim(version) for version in range(3, protocol_version)}
        if not required <= protected.keys():
            raise ValueError("Protocol must retain all consumed prior repair claims")
    for name, expected_digest in protected.items():
        if not isinstance(name, str) or not isinstance(expected_digest, str):
            raise ValueError("Invalid protected evidence binding")
        if protocol_version >= 5:
            # Frozen artifacts include SQLite/source/checkpoints, not only JSON receipts.
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Protected evidence escaped evaluation output")
            actual_digest = hashlib.sha256(read_bounded(target)).hexdigest()
        else:
            _, actual_digest = read_public(root / name, root)
        if actual_digest != expected_digest:
            raise ValueError("Prior evidence/approval changed; no dispatch permitted")
    _, current_fixture = load_fixture(
        repo_root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    if proposal.get("fixture_sha256") != current_fixture:
        raise ValueError("Approved fixture changed")
    return {
        "path": path.resolve().relative_to(root).as_posix(),
        "sha256": digest,
        "git_revision": proposal["git_revision"],
    }


def prepare_repairs(
    repo_root: Path, protocol_version: int = 3, proposal: Path | None = None
) -> Path:
    """No dotenv, Docker, provider initialization, claim creation or network request."""
    contract = repair_contract(protocol_version)
    binding = None
    if protocol_version >= 4:
        if proposal is None:
            raise ValueError(
                f"Protocol {protocol_version} requires the separately approved proposal"
            )
        binding = approved_proposal(proposal, repo_root, protocol_version)
    revision, dirty = _git_state(repo_root)
    _, digest = load_fixture(
        repo_root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    directory = repo_root / "data/evaluation" / ("repair-plan-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    plan: dict[str, Any] = {
        "contract": contract,
        "git_revision": revision,
        "source_ready": bool(revision and dirty is False),
        "fixture_sha256": digest,
        "external_api_calls": 0,
        "paid_execution_approved": False,
    }
    if binding is not None:
        plan["approved_proposal"] = binding
    _write_json(directory / "plan.json", plan)
    text = (
        f"# Phase 10 targeted repair verification v{protocol_version}\n\n"
        "Preparation only: zero external calls. Separate owner approval is required to run.\n\n"
        "| Task | Required observation |\n|---|---|\n"
        + "\n".join(
            f"| {task} | {plan['contract']['acceptance'][task]} |" for task in contract["tasks"]
        )
        + f"\n\nAt most {contract['physical_limits']['llm']} physical LLM requests, "
        "12 per task, 0 Tavily; corrections count. "
        "120000-token stopping threshold per task; the final returned response can exceed it. "
        "Output cap 8192 per response. No automatic task replays. "
        "Only public MNIST objectives, seeded code, public evidence and results are sent. "
        "Closed-corpus retrieval and seeded code delimit this test.\n\n"
        "Preparation does not consume or reset an old grant. Previous scores and unknowns "
        "remain separate. Passing this check is not full Phase 10 acceptance or main approval.\n\n"
        f"Source ready: {plan['source_ready']}; commit: {revision}.\n"
    )
    (directory / "evaluation_report.md").write_text(text, encoding="utf-8")
    return directory


def load_repair_plan(path: Path, repo_root: Path, protocol_version: int = 3) -> dict[str, Any]:
    root = (repo_root / "data/evaluation").resolve()
    plan, _ = read_public(path.resolve(), root)
    revision, dirty = _git_state(repo_root)
    _, digest = load_fixture(
        repo_root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    if (
        not isinstance(plan, dict)
        or plan.get("contract") != repair_contract(protocol_version)
        or plan.get("source_ready") is not True
        or not revision
        or dirty is not False
        or plan.get("git_revision") != revision
        or plan.get("fixture_sha256") != digest
        or plan.get("external_api_calls") != 0
        or plan.get("paid_execution_approved") is not False
    ):
        raise ValueError("Repair plan/source changed; prepare and review a new clean-source plan")
    if protocol_version >= 4:
        binding = plan.get("approved_proposal")
        if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
            raise ValueError("Plan lacks an approved proposal binding")
        if approved_proposal(root / binding["path"], repo_root, protocol_version) != binding:
            raise ValueError("Approved proposal changed; no dispatch permitted")
    return plan


def physical_accounting(record: dict[str, Any], provider: RepairWorkflowProvider) -> bool:
    attempts = [
        row
        for row in provider.transport.ledger.attempts
        if row["case_id"].startswith(provider.task + "--")
    ]
    usage = record.get("usage", {})
    return bool(
        attempts
        and provider.calls == len(attempts) == usage.get("model_calls", 0)
        and all(row["state"] == "returned" and row["http_status"] == 200 for row in attempts)
        and all(
            all(row["usage"][key] is not None for row in attempts)
            and sum(row["usage"][key] for row in attempts) == usage.get(key, 0)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
    )


def repair_summary(
    records: list[dict[str, Any]],
    ledger: RepairLedger,
    *,
    clean: bool,
    unchanged: bool,
    protocol_version: int = 3,
) -> dict[str, Any]:
    summary = pilot_summary(records, ledger, clean=clean, unchanged=unchanged)
    contract = repair_contract(protocol_version)
    contracts = summary["scenario_contracts"]
    for row in records:
        counts, usage = row.get("step_counts", {}), row.get("usage", {})
        single_upstream = counts.get("research") == counts.get("code") == 1
        expected_train = 2 if row["task"] == "recover-baseline" else 1
        contracts[row["task"]] &= bool(
            single_upstream
            and counts.get("train") == expected_train
            and usage.get("training_runs") == expected_train
            and usage.get("code_repairs", 0) == 0
            and row.get("fault_mutations") == (0 if row["task"] == "normal-baseline" else 1)
        )
    complete = (
        [row["task"] for row in records] == contract["tasks"]
        and bool(ledger.attempts)
        and all(
            row.get("status") == "measured"
            and row.get("physical_accounting_verified")
            and row.get("intervention_verified")
            and row.get("task_success") is not None
            for row in records
        )
        and clean
        and unchanged
    )
    summary.update(
        protocol_version=protocol_version,
        planned_tasks=len(contract["tasks"]),
        repair_measurement_complete=bool(complete),
        targeted_repairs_acceptance=bool(complete and all(contracts.values())),
        scope=contract["limitations"],
    )
    return summary


async def run_repairs(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    if not args.allow_paid or not args.plan or not args.approval_id or not args.sandbox_container:
        raise ValueError(
            "Paid repairs require separate approval, exact prepared plan and controller"
        )
    version = getattr(args, "protocol_version", 3)
    plan = load_repair_plan(args.plan, repo_root, version)
    root = (repo_root / "data/evaluation").resolve()
    grant = approval_path(root, args.approval_id)
    claim = root / repair_claim(version)
    if grant.exists() or claim.exists():
        raise ValueError("Repair verification already claimed; do not reset/replay with a new ID")
    settings, _ = load_explicit_settings(repo_root / ".env", require_tavily=False)
    endpoint = httpx.URL(settings.llm_base_url.rstrip("/") + "/chat/completions")
    if endpoint.scheme != "https" or endpoint.userinfo or endpoint.query or endpoint.fragment:
        raise ValueError("Use an explicit credential-free HTTPS provider endpoint")
    fixture, _ = load_fixture(
        repo_root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    assert settings.llm_api_key is not None
    sandbox = DockerEvaluationSandbox(args.sandbox_container)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    if not await no_owned_children(sandbox):
        raise ValueError("Dedicated controller already owns children")
    directory = root / (f"live-repairs-v{version}-" + uuid4().hex)
    directory.mkdir(exist_ok=False)
    manifest = {
        "protocol_version": version,
        "git_revision": plan["git_revision"],
        "prepared_plan": plan,
        "plan_sha256": payload_digest(plan),
        "approval_id": args.approval_id,
        "explicit_paid_execution": True,
        "resources": resources,
        "configured_model": settings.llm_model,
    }
    _write_json(directory / "manifest.json", manifest)
    with claim.open("x", encoding="utf-8") as stream:
        json.dump({"run_id": directory.name, "plan_sha256": payload_digest(plan)}, stream)
    ledger = RepairLedger(grant, run_id=directory.name, protocol_version=version)
    transport = GuardedTransport(
        provider="llm",
        endpoint=str(endpoint),
        ledger=ledger,
        output=directory,
        secrets=(settings.llm_api_key.get_secret_value(),),
        max_output_tokens=OUTPUT_TOKENS,
    )
    records: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=60
        ) as client:
            native = OpenAICompatibleProvider(
                settings,
                http_client=client,
                request_validator=transport.validate_payload,
            )
            for task in plan["contract"]["tasks"]:
                load_repair_plan(args.plan, repo_root, version)
                scenario, _, variant_name = task.partition("-")
                variant: WorkflowVariant = (
                    "no_replanning" if variant_name == "no_replanning" else "baseline"
                )
                script = fixture.scripts[scenario].model_copy(
                    update={"prompt": fixture.scripts[scenario].prompt + SUFFIX}
                )
                target = directory / task
                target.mkdir(exist_ok=False)
                provider = RepairWorkflowProvider(native, transport, task)
                provider.verify_source = lambda: load_repair_plan(args.plan, repo_root, version)
                record: dict[str, Any] = {
                    "task": task,
                    "status": "started",
                    "task_success": None,
                    "input_sha256": payload_digest(
                        {
                            "prompt": script.prompt,
                            "specification": script.specification.model_dump(),
                            "fault": script.fault,
                            "contract": plan["contract"],
                            "resources": resources,
                        }
                    ),
                }
                records.append(record)
                _write_json(directory / "cases.json", records)
                print(
                    f"Targeted repair v{version} {task}; "
                    f"LLM {len(ledger.attempts)}/{ledger.limits['llm']}",
                    flush=True,
                )
                try:
                    async with asyncio.timeout(660):
                        record.update(
                            await execute_task(
                                directory=target,
                                fixture=fixture,
                                script=script,
                                variant=variant,
                                provider=provider,
                                sandbox=sandbox,
                                resources=resources,
                                protocol_version=version,
                            )
                        )
                finally:
                    record["physical_requests"] = sum(
                        row["case_id"].startswith(task + "--") for row in ledger.attempts
                    )
                    record["native_json_corrections"] = provider.corrections
                    record["physical_accounting_verified"] = physical_accounting(record, provider)
                    _write_json(directory / "cases.json", records)
                if (
                    provider.failed
                    or record["status"] != "measured"
                    or not record["physical_accounting_verified"]
                    or not await no_owned_children(sandbox)
                ):
                    raise ValueError(
                        "Repair verification stopped: provider/accounting/resource fault"
                    )
    except Exception as exc:
        print(f"Repair verification stopped ({type(exc).__name__}); no task replay", flush=True)
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        _write_json(directory / "request_ledger.json", ledger.data)
        summary = repair_summary(
            records,
            ledger,
            clean=clean,
            unchanged=_git_state(repo_root) == (plan["git_revision"], False),
            protocol_version=version,
        )
        _write_json(directory / "acceptance.json", summary)
        report = (
            f"# Phase 10 targeted repair verification v{version}\n\n"
            f"Targeted repair acceptance: {summary['targeted_repairs_acceptance']}.\n\n"
            f"Physical LLM requests: {len(ledger.attempts)}/{ledger.limits['llm']}; Tavily: 0.\n\n"
            + "\n".join(
                f"- {row['task']}: {summary['scenario_contracts'][row['task']]}" for row in records
            )
            + "\n\n"
            + summary["scope"]
            + "\n\nFull Phase 10 and main merge remain unapproved.\n"
        )
        (directory / "evaluation_report.md").write_text(report, encoding="utf-8")
    return directory, bool(summary["targeted_repairs_acceptance"])
