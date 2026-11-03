"""Offline preparation and separately approved one-shot paired v8 execution."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from autoscholar.core.config import Settings
from autoscholar.evaluation.baseline_protocol import (
    CLAIM as BASELINE_CLAIM,
)
from autoscholar.evaluation.baseline_protocol import (
    COMPATIBILITY_CLAIM,
    BaselineLedger,
    baseline_contract,
    baseline_summary,
)
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
from autoscholar.llm.compatibility import resolve_profile
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider


def selected_contract(version: int) -> dict[str, Any]:
    if type(version) is not int or version not in (8, 9, 10):
        raise ValueError("Only paired v8 or single baselines v9/v10 supported")
    if version == 10:
        return baseline_contract(10)
    return baseline_contract() if version == 9 else paired_contract()


def provider_binding(settings: Settings) -> dict[str, str]:
    endpoint = httpx.URL(settings.llm_base_url.rstrip("/") + "/chat/completions")
    if endpoint.scheme != "https" or endpoint.userinfo or endpoint.query or endpoint.fragment:
        raise ValueError("Use a credential-free HTTPS endpoint")
    profile = resolve_profile(settings.llm_compatibility_profile, settings.llm_base_url)
    if profile != "deepseek" or not settings.llm_model:
        raise ValueError("v10 requires a configured model and the repaired DeepSeek profile")
    return {
        "configured_model": settings.llm_model,
        "configured_profile": settings.llm_compatibility_profile,
        "effective_profile": profile,
        "endpoint_sha256": payload_digest(str(endpoint)),
    }


def verify_provider(plan: dict[str, Any], settings: Settings) -> None:
    if provider_binding(settings) != plan.get("provider_binding"):
        raise ValueError("Frozen provider endpoint/model/profile changed; no dispatch")


def checked_history(history: Path, base: Path, version: int) -> dict[str, str]:
    protected, binding = history_snapshot(history, base)
    if version in (9, 10) and CLAIM not in protected:
        raise ValueError("Baseline v9 must protect the consumed v8 claim")
    if version == 10 and BASELINE_CLAIM not in protected:
        raise ValueError("Baseline v10 must protect the consumed v9 claim")
    return binding


def prepare_paired(root: Path, history: Path, version: int = 8) -> Path:
    contract = selected_contract(version)
    revision, dirty = _git_state(root)
    if not revision or dirty is not False:
        raise ValueError("Prepare a clean committed source tree")
    binding = checked_history(history, root / "data/evaluation", version)
    _, fixture = load_fixture(
        root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    configuration = None
    if version == 10:
        settings, _ = load_explicit_settings(root / ".env", require_tavily=False)
        configuration = provider_binding(settings)
    directory = root / "data/evaluation" / ("paired-plan-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    prepared = {
        "schema_version": 1,
        "contract": contract,
        "git_revision": revision,
        "fixture_sha256": fixture,
        "history_index": binding,
        "source_ready": True,
        "external_api_calls": 0,
        "paid_execution_approved": False,
    }
    if configuration is not None:
        prepared["provider_binding"] = configuration
    _write_json(directory / "plan.json", prepared)
    return directory


def load_paired_plan(path: Path, root: Path, version: int = 8) -> dict[str, Any]:
    contract = selected_contract(version)
    base = root / "data/evaluation"
    plan, _ = read_public(path.resolve(), base)
    revision, dirty = _git_state(root)
    _, fixture = load_fixture(
        root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or plan.get("contract") != contract
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
    if checked_history(base / binding["path"], base, version) != binding:
        raise ValueError("Historical index changed")
    if version == 10:
        settings, _ = load_explicit_settings(root / ".env", require_tavily=False)
        verify_provider(plan, settings)
    return plan


async def run_paired(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    version = getattr(args, "protocol_version", 8)
    contract = selected_contract(version)
    cap = contract["physical_limits"]["llm"]
    label = f"Baseline v{version}" if version in (9, 10) else "Paired v8"
    if not args.allow_paid or not args.plan or not args.approval_id or not args.sandbox_container:
        raise ValueError(
            "Paid paired execution requires separate approval, exact plan and controller"
        )
    plan = load_paired_plan(args.plan, repo_root, version)
    base = (repo_root / "data/evaluation").resolve()
    claim_name = COMPATIBILITY_CLAIM if version == 10 else BASELINE_CLAIM if version == 9 else CLAIM
    claim = base / claim_name
    grant = approval_path(base, args.approval_id)
    if claim.exists() or grant.exists():
        raise ValueError(f"{label} already claimed; no reset or task replay")
    settings, _ = load_explicit_settings(repo_root / ".env", require_tavily=False)
    if version == 10:
        verify_provider(plan, settings)

    def verify_run_plan() -> dict[str, Any]:
        current = load_paired_plan(args.plan, repo_root, version)
        if version == 10:
            if current != plan:
                raise ValueError("Frozen execution plan changed; no dispatch")
            verify_provider(current, settings)
        return current

    endpoint = httpx.URL(settings.llm_base_url.rstrip("/") + "/chat/completions")
    if endpoint.scheme != "https" or endpoint.userinfo or endpoint.query or endpoint.fragment:
        raise ValueError("Use a credential-free HTTPS endpoint")
    assert settings.llm_api_key is not None
    fixture, _ = load_fixture(
        repo_root / "benchmarks/ablations/workflow_fixture_v2.json", WorkflowFixture
    )
    sandbox = DockerEvaluationSandbox(args.sandbox_container)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    if version in (9, 10) and resources != contract["resources"]:
        await sandbox.close()
        raise ValueError("Frozen baseline resources changed")
    if not await no_owned_children(sandbox):
        raise ValueError("Dedicated controller already owns children")
    prefix = f"live-baseline-v{version}-" if version in (9, 10) else "live-paired-v8-"
    directory = base / (prefix + uuid4().hex)
    directory.mkdir(exist_ok=False)
    _write_json(
        directory / "manifest.json",
        {
            "protocol_version": version,
            "git_revision": plan["git_revision"],
            "prepared_plan": plan,
            "plan_sha256": payload_digest(plan),
            "approval_id": args.approval_id,
            "explicit_paid_execution": True,
            "configured_model": settings.llm_model,
            **({"provider_binding": provider_binding(settings)} if version == 10 else {}),
            "resources": resources,
        },
    )
    with claim.open("x", encoding="utf-8") as stream:
        json.dump({"run_id": directory.name, "plan_sha256": payload_digest(plan)}, stream)
    ledger = (
        BaselineLedger(grant, run_id=directory.name, protocol_version=version)
        if version in (9, 10)
        else PairedLedger(grant, run_id=directory.name)
    )
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
                verify_run_plan()
                scenario, _, variant = task.partition("-")
                if variant not in WORKFLOW_VARIANTS:
                    raise ValueError("Unknown frozen variant")
                script = fixture.scripts[scenario].model_copy(
                    update={"prompt": fixture.scripts[scenario].prompt + SUFFIX}
                )
                provider = RepairWorkflowProvider(native, transport, task)
                provider.verify_source = verify_run_plan
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
                print(f"{label} {task}; LLM {len(ledger.attempts)}/{cap}", flush=True)
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
                    or not record.get("intervention_verified")
                    or not baseline_passed(record)
                    or len(record.get("observed_models", [])) != 1
                    or record["observed_models"] != records[0].get("observed_models")
                    or not await no_owned_children(sandbox)
                ):
                    raise ValueError(
                        "Baseline/provider/accounting/intervention/identity/cleanup gate failed"
                    )
                record["execution_gates_passed"] = True
                _write_json(directory / "cases.json", records)
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
            verify_run_plan()
            unchanged = True
        except (OSError, ValueError):
            unchanged = False
        if isinstance(ledger, BaselineLedger):
            summary = baseline_summary(records, ledger, clean=clean, unchanged=unchanged)
            accepted = bool(summary["baseline_acceptance"])
        else:
            summary = paired_summary(records, ledger, clean=clean, unchanged=unchanged)
            accepted = bool(summary["paired_pilot_acceptance"])
        _write_json(directory / "acceptance.json", summary)
        (directory / "evaluation_report.md").write_text(
            f"# {label}\n\nAcceptance: {accepted}. "
            f"Physical LLM requests: {len(ledger.attempts)}/{cap}; Tavily 0.\n\n"
            + summary["scope"],
            encoding="utf-8",
        )
    return directory, accepted
