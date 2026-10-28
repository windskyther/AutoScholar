"""Offline preparation and separately approved one-shot paired v8 execution."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.live_ablation import execute_task
from autoscholar.evaluation.live_ablation_contract import LIMITS
from autoscholar.evaluation.live_ablation_v2 import SUFFIX
from autoscholar.evaluation.paired_protocol import (
    CLAIM,
    PairedLedger,
    baseline_passed,
    history_snapshot,
    paired_contract,
    paired_summary,
)
from autoscholar.evaluation.provider_prior import read_public
from autoscholar.evaluation.provider_transport import GuardedTransport, approval_path
from autoscholar.evaluation.provider_validation import load_explicit_settings, no_owned_children
from autoscholar.evaluation.repair_protocol import RepairWorkflowProvider
from autoscholar.evaluation.repair_validation import physical_accounting
from autoscholar.evaluation.runner import _git_state, _write_json
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS
from autoscholar.evaluation.workflow_fixture import WorkflowFixture
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider


def prepare_paired(root: Path, history: Path) -> Path:
    revision, dirty = _git_state(root)
    if not revision or dirty is not False:
        raise ValueError("Prepare a clean committed source tree")
    _, binding = history_snapshot(history, root / "data/evaluation")
    _, fixture = load_fixture(
        root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    directory = root / "data/evaluation" / ("paired-plan-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    _write_json(
        directory / "plan.json",
        {
            "schema_version": 1,
            "contract": paired_contract(),
            "git_revision": revision,
            "fixture_sha256": fixture,
            "history_index": binding,
            "source_ready": True,
            "external_api_calls": 0,
            "paid_execution_approved": False,
        },
    )
    return directory


def load_paired_plan(path: Path, root: Path) -> dict[str, Any]:
    base = root / "data/evaluation"
    plan, _ = read_public(path.resolve(), base)
    revision, dirty = _git_state(root)
    _, fixture = load_fixture(
        root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or plan.get("contract") != paired_contract()
        or not revision
        or dirty is not False
        or plan.get("git_revision") != revision
        or plan.get("fixture_sha256") != fixture
        or plan.get("source_ready") is not True
        or plan.get("external_api_calls") != 0
        or plan.get("paid_execution_approved") is not False
    ):
        raise ValueError("Paired plan/source/fixture changed")
    binding = plan.get("history_index")
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
        raise ValueError("Missing historical binding")
    if history_snapshot(base / binding["path"], base)[1] != binding:
        raise ValueError("Historical index changed")
    return plan


async def run_paired(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    if not args.allow_paid or not args.plan or not args.approval_id or not args.sandbox_container:
        raise ValueError(
            "Paid paired execution requires separate approval, exact plan and controller"
        )
    plan = load_paired_plan(args.plan, repo_root)
    base = (repo_root / "data/evaluation").resolve()
    claim, grant = base / CLAIM, approval_path(base, args.approval_id)
    if claim.exists() or grant.exists():
        raise ValueError("Paired v8 already claimed; no reset or task replay")
    settings, _ = load_explicit_settings(repo_root / ".env", require_tavily=False)
    endpoint = httpx.URL(settings.llm_base_url.rstrip("/") + "/chat/completions")
    if endpoint.scheme != "https" or endpoint.userinfo or endpoint.query or endpoint.fragment:
        raise ValueError("Use a credential-free HTTPS endpoint")
    assert settings.llm_api_key is not None
    fixture, _ = load_fixture(
        repo_root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    sandbox = DockerEvaluationSandbox(args.sandbox_container)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    if not await no_owned_children(sandbox):
        raise ValueError("Dedicated controller already owns children")
    directory = base / ("live-paired-v8-" + uuid4().hex)
    directory.mkdir(exist_ok=False)
    _write_json(
        directory / "manifest.json",
        {
            "protocol_version": 8,
            "git_revision": plan["git_revision"],
            "prepared_plan": plan,
            "plan_sha256": payload_digest(plan),
            "approval_id": args.approval_id,
            "explicit_paid_execution": True,
            "configured_model": settings.llm_model,
            "resources": resources,
        },
    )
    with claim.open("x", encoding="utf-8") as stream:
        json.dump({"run_id": directory.name, "plan_sha256": payload_digest(plan)}, stream)
    ledger = PairedLedger(grant, run_id=directory.name)
    transport = GuardedTransport(
        provider="llm",
        endpoint=str(endpoint),
        ledger=ledger,
        output=directory,
        secrets=(settings.llm_api_key.get_secret_value(),),
        max_output_tokens=8192,
    )
    records: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=60
        ) as client:
            native = OpenAICompatibleProvider(
                settings, http_client=client, request_validator=transport.validate_payload
            )
            for task in plan["contract"]["tasks"]:
                load_paired_plan(args.plan, repo_root)
                scenario, _, variant = task.partition("-")
                if variant not in WORKFLOW_VARIANTS:
                    raise ValueError("Unknown frozen variant")
                script = fixture.scripts[scenario].model_copy(
                    update={"prompt": fixture.scripts[scenario].prompt + SUFFIX}
                )
                provider = RepairWorkflowProvider(native, transport, task)
                provider.verify_source = lambda: load_paired_plan(args.plan, repo_root)
                target = directory / task
                target.mkdir(exist_ok=False)
                record: dict[str, Any] = {
                    "task": task,
                    "scenario": scenario,
                    "variant": variant,
                    "status": "started",
                    "task_success": None,
                    "input_sha256": payload_digest(
                        {
                            "prompt": script.prompt,
                            "specification": script.specification.model_dump(),
                            "fault": script.fault,
                            "limits": LIMITS.model_dump(),
                            "resources": resources,
                            "fixture": plan["fixture_sha256"],
                            "source": plan["git_revision"],
                        }
                    ),
                }
                records.append(record)
                _write_json(directory / "cases.json", records)
                print(f"Paired v8 {task}; LLM {len(ledger.attempts)}/180", flush=True)
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
                                protocol_version=8,
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
                    or not record.get("intervention_verified")
                    or not baseline_passed(record)
                    or len(record.get("observed_models", [])) != 1
                    or record["observed_models"] != records[0].get("observed_models")
                    or not await no_owned_children(sandbox)
                ):
                    raise ValueError(
                        "Baseline/provider/accounting/intervention/identity/cleanup gate failed"
                    )
    except Exception as exc:
        print(
            f"Paired pilot stopped ({type(exc).__name__}); partial evidence retained; no replay",
            flush=True,
        )
    finally:
        clean = await no_owned_children(sandbox)
        await sandbox.close()
        _write_json(directory / "request_ledger.json", ledger.data)
        try:
            load_paired_plan(args.plan, repo_root)
            unchanged = True
        except (OSError, ValueError):
            unchanged = False
        summary = paired_summary(records, ledger, clean=clean, unchanged=unchanged)
        _write_json(directory / "acceptance.json", summary)
        (directory / "evaluation_report.md").write_text(
            f"# Paired v8\n\nAcceptance: {summary['paired_pilot_acceptance']}. "
            f"Physical LLM requests: {len(ledger.attempts)}/180; Tavily 0.\n\n" + summary["scope"],
            encoding="utf-8",
        )
    return directory, bool(summary["paired_pilot_acceptance"])
