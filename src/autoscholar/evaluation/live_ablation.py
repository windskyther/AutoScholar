"""Explicit one-shot live LLM workflow ablations in isolated local durable state."""

import argparse
import asyncio
import json
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from autoscholar.coding.sandbox import SandboxExecutor
from autoscholar.evaluation.ablation_audit import holm_adjust
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.e2e_adapter import initial_plan_is_static
from autoscholar.evaluation.experiment_adapter import verify_completed_experiment
from autoscholar.evaluation.isolated_components import ObservedSandbox, sandbox_resources
from autoscholar.evaluation.live_ablation_continuation import (
    CLAIM_NAME,
    CONTINUATION_TASKS,
    HISTORICAL_REQUESTS,
    NEW_REQUEST_LIMIT,
    ContinuationLedger,
    StoppedV2,
)
from autoscholar.evaluation.live_ablation_contract import (
    LIMITS,
    OBJECTIVE_SUFFIX,
    SCENARIOS,
    TASKS,
    ClosedCorpusSearch,
    LiveAblationLedger,
    LiveWorkflowProvider,
)
from autoscholar.evaluation.live_ablation_v2 import (
    OUTPUT_TOKENS,
    PRIOR_REQUESTS,
    REMAINING_REQUESTS,
    SUFFIX,
    DiverseCorpusSearch,
    ExtractiveWorkflowProvider,
    PriorPilot,
    RemainingPilotLedger,
    plan_roles,
)
from autoscholar.evaluation.provider_transport import GuardedTransport, approval_path
from autoscholar.evaluation.provider_validation import load_explicit_settings, no_owned_children
from autoscholar.evaluation.runner import _git_state, _write_json
from autoscholar.evaluation.workflow_ablation import (
    WORKFLOW_VARIANTS,
    ObservedWorkflowMemory,
    WorkflowVariant,
    public_project_context,
)
from autoscholar.evaluation.workflow_fixture import (
    WorkflowFaultSandbox,
    WorkflowFixture,
    WorkflowScript,
)
from autoscholar.evaluation.workflow_knowledge import PUBLIC_KNOWLEDGE, workflow_knowledge
from autoscholar.evaluation.workflow_runtime import local_workflow, workflow_service
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.orchestration.memory import ProjectMemoryUpdate
from autoscholar.orchestration.service import source_digest
from autoscholar.rag.repository import KnowledgeRepository


async def execute_task(
    *,
    directory: Path,
    fixture: WorkflowFixture,
    script: WorkflowScript,
    variant: WorkflowVariant,
    provider: LiveWorkflowProvider,
    sandbox: SandboxExecutor,
    resources: dict[str, str],
    protocol_version: int = 1,
) -> dict[str, Any]:
    recorded = ObservedSandbox(sandbox, resources)
    faulted = WorkflowFaultSandbox(recorded, script.fault)
    search = (DiverseCorpusSearch if protocol_version >= 2 else ClosedCorpusSearch)(fixture)
    async with local_workflow(directory) as state, AsyncExitStack() as stack:
        project = await KnowledgeRepository(state.tasks.session_factory).create_project(
            name="Public live ablation",
            description="Isolated closed-corpus MNIST pilot",
        )
        knowledge = await stack.enter_async_context(workflow_knowledge(state, project.id))
        durable = workflow_service(
            state,
            provider,
            search,
            faulted,
            script,
            variant=variant,
            observe_memory=True,
            knowledge=knowledge,
            limits=LIMITS,
        )
        await durable.memory.update(
            project.id,
            ProjectMemoryUpdate(expected_version=0, context=public_project_context()),
        )
        task_id, _ = await durable.submit(
            {
                "objective": script.prompt,
                "research_sources": ["web"],
                "experiment_specification": script.specification.model_dump(),
                "budget": LIMITS.model_dump(),
                "project_id": project.id,
            },
            "public-live-ablation",
        )
        for _ in range(20):
            progressing = await durable.tick(task_id)
            snapshot, job = await durable.repository.snapshot(task_id)
            if provider.failed or not progressing or job.status != "queued":
                break
        snapshot, job = await durable.repository.snapshot(task_id)
        parent = await state.tasks.get_task(task_id)
        if parent is None:
            raise ValueError("Missing persisted live parent")
        steps = await durable.service.workflows.history(task_id, "steps")
        plans = await durable.service.workflows.history(task_id, "plans")
        reviews = await durable.service.workflows.history(task_id, "reviews")
        checkpoints = await durable.repository.history(task_id, "checkpoints")
        events = await durable.repository.history(task_id, "events", limit=500)
        _write_json(
            directory / "workflow.json",
            {
                "task_id": task_id,
                "database": state.database.relative_to(directory).as_posix(),
                "snapshot": snapshot.model_dump(mode="json"),
                "usage": job.usage,
                "status": parent.status,
                "error_code": parent.error_code,
                "answer": parent.answer,
                "steps": steps,
                "plans": plans,
                "reviews": reviews,
                "checkpoints": checkpoints,
                "events": events,
                "searches": search.records,
            },
        )
        roles = (
            plan_roles(snapshot.plan)
            if protocol_version >= 2
            else {
                "research": "research",
                "code": "code",
                "train": "train",
            }
        )
        results = {
            role: snapshot.results[identifier]
            for role, identifier in roles.items()
            if identifier in snapshot.results
        }
        expected_types = {"research": "research", "code": "coding", "train": "experiment"}
        plan_valid = snapshot.plan is not None and (
            bool(roles)
            if protocol_version >= 2
            else {step.id: step.type for step in snapshot.plan.steps} == expected_types
        )
        completed_steps = (
            plan_valid
            and set(results) == set(expected_types)
            and all(row["status"] == "succeeded" for row in results.values())
        )
        trace_bound = parent.status == job.status
        for step in steps:
            child = await state.tasks.get_task(step["child_task_id"])
            trace_bound = (
                trace_bound
                and child is not None
                and (
                    child.parent_task_id == task_id
                    and child.status == step["status"]
                    and step["result"].get("child_task_id") == child.id
                )
            )
        evidence_bound = False
        evidence_references: list[str] = []
        research_result = results.get("research", {})
        if research_result.get("status") == "succeeded":
            child = await state.tasks.get_task(research_result["child_task_id"])
            assert child is not None
            accepted_sources = {
                f"https://example.org/autoscholar-public/{topic}": source.content
                for topic, source in fixture.sources.items()
            }
            evidence = {item.citation_key: item for item in child.evidence}
            cited = {key for citation in child.citations for key in citation.evidence_ids}
            evidence_bound = bool(evidence and child.citations) and cited <= evidence.keys()
            for item in evidence.values():
                source = (
                    PUBLIC_KNOWLEDGE
                    if (
                        item.source_type == "document"
                        and item.document_id
                        and item.chunk_id
                        and item.url
                        == f"/projects/{project.id}/documents/{item.document_id}/content#page=1"
                    )
                    else accepted_sources.get(item.url or "")
                )
                evidence_bound = bool(
                    evidence_bound
                    and source
                    and item.claim
                    and item.excerpt
                    and item.claim in source
                    and item.excerpt in source
                )
                evidence_references.append(f"{child.id}:{item.citation_key}")
            for citation in child.citations:
                evidence_bound = (
                    evidence_bound
                    and bool(citation.claim)
                    and all(
                        key in evidence
                        and citation.claim in evidence[key].excerpt
                        and f"[{key}]" in (child.answer or "")
                        for key in citation.evidence_ids
                    )
                )
        code_result, train_result = results.get("code", {}), results.get("train", {})
        checks_passed, handoff = False, False
        if code_result.get("status") == "succeeded":
            validations = [
                (request, result)
                for request, result in recorded.runs
                if request.task_id == code_result["child_task_id"]
                and request.action in ("static_check", "run_pytest")
            ]
            checks_passed = {req.action for req, _ in validations} == {
                "static_check",
                "run_pytest",
            } and all(
                result.status == "succeeded"
                and result.exit_code == 0
                and not result.truncated
                and source_digest(request.files) == code_result["source_sha256"]
                for request, result in validations
            )
        execution_count = len(recorded.runs)
        verification = None
        report_bound = False
        latest_only = False
        if train_result.get("status") == "succeeded" and code_result.get("status") == "succeeded":
            code_source = state.workspace.source_snapshot(code_result["child_task_id"])
            transferred = {
                **code_source,
                "experiment_config.json": json.dumps(script.specification.model_dump(), indent=2),
            }
            handoff = (
                source_digest(code_source) == code_result["source_sha256"]
                and state.workspace.source_snapshot(train_result["child_task_id"]) == transferred
                and train_result["source_sha256"]
                == train_result["expected_source_sha256"]
                == source_digest(transferred)
            )
            verification = await verify_completed_experiment(
                task_id=train_result["child_task_id"],
                store=state.tasks,
                workspace=state.workspace,
                sandbox=recorded,
                resources=resources,
                specification=script.specification,
                train_source=(Path(__file__).parents[1] / "experiment/train_template.py").read_text(
                    encoding="utf-8"
                ),
                oracle_source=Path(__file__)
                .with_name("experiment_oracle.py")
                .read_text(encoding="utf-8"),
            )
            child = await state.tasks.get_task(train_result["child_task_id"])
            answer = parent.answer or ""
            report_bound = bool(
                child
                and child.answer
                and child.answer in answer
                and f"Experiment: {train_result['experiment_id']}" in answer
                and f"Source SHA-256: {source_digest(transferred)}" in answer
                and f"Review: PASS; plan version: {snapshot.version}." in answer
                and evidence_references
                and all(ref in answer for ref in evidence_references)
            )
            earlier = [
                step
                for step in steps
                if step["step_id"] == roles.get("train")
                and step["child_task_id"] != train_result["child_task_id"]
            ]
            latest_only = bool(answer) and all(
                step["child_task_id"] not in answer for step in earlier
            )
            for step in earlier:
                latest_only = latest_only and all(
                    item.id not in answer
                    for item in await state.tasks.list_experiments(step["child_task_id"])
                )
        memory = durable.memory
        assert isinstance(memory, ObservedWorkflowMemory)
        paths = {
            "stage_calls": provider.stage_calls,
            "memory_reads": memory.reads,
            "memory_contexts": memory.nonempty,
            "memory_payloads": provider.memory_payloads,
            "memory_learning_calls": memory.learns,
            "static_plan": initial_plan_is_static(plans),
            "saved_plans": len(plans),
            "saved_reviews": len(reviews),
        }
        intervention = (
            (
                variant != "no_planner"
                or (paths["static_plan"] and not provider.stage_calls.get("submit_task_plan"))
            )
            and (variant != "no_reviewer" or not provider.stage_calls.get("submit_review"))
            and (
                variant != "no_memory"
                or not (memory.reads or memory.learns or provider.memory_payloads)
            )
            and (variant != "no_replanning" or not provider.stage_calls.get("submit_plan_revision"))
        )
        checks = {
            "trace_bound": trace_bound,
            "completed_steps": completed_steps,
            "evidence_bound": evidence_bound,
            "code_validated": checks_passed,
            "source_handoff": handoff,
            "report_bound": report_bound,
            "latest_results_only": latest_only,
            "independent_checkpoint_oracle": bool(verification and verification.accepted),
        }
        incomplete = (
            protocol_version >= 2
            and job.status
            in {"queued", "running", "pause_requested", "cancel_requested", "recovery_required"}
            and not provider.failed
        )
        return {
            "status": "infrastructure_incomplete"
            if incomplete
            else ("measured" if not provider.failed else "provider_error"),
            "workflow_status": parent.status,
            "error_code": parent.error_code,
            "task_success": parent.status == "succeeded" and all(checks.values())
            if not provider.failed and not incomplete
            else None,
            "checks": checks,
            "intervention_verified": intervention,
            "paths": paths,
            "usage": job.usage,
            "search_queries": search.calls,
            "fault_mutations": faulted.mutations,
            "execution_sandbox_runs": execution_count,
            "grading_sandbox_runs": len(recorded.runs) - execution_count,
            "verification": verification.model_dump(mode="json") if verification else None,
            "observed_models": sorted(provider.models),
            "step_counts": {
                key: sum(step["step_id"] == roles.get(key) for step in steps)
                for key in expected_types
            },
        }


def pilot_summary(
    records: list[dict[str, Any]], ledger: LiveAblationLedger, *, clean: bool, unchanged: bool
) -> dict[str, Any]:
    by_id = {row["task"]: row for row in records}
    contrasts: list[dict[str, Any]] = []
    for variant in WORKFLOW_VARIANTS[1:]:
        deltas = []
        for scenario in SCENARIOS:
            left, right = by_id.get(f"{scenario}-baseline"), by_id.get(f"{scenario}-{variant}")
            if (
                not left
                or not right
                or left.get("task_success") is None
                or right.get("task_success") is None
            ):
                continue
            if left["input_sha256"] != right["input_sha256"] or left.get(
                "observed_models"
            ) != right.get("observed_models"):
                continue
            deltas.append(int(right["task_success"]) - int(left["task_success"]))
        discordant = sum(bool(value) for value in deltas)
        # Exact paired binary sign test, two-sided; n <= 3. Missing pairs are not failures.
        from math import comb

        minority = min(sum(value > 0 for value in deltas), sum(value < 0 for value in deltas))
        pvalue = min(1.0, 2 * sum(comb(discordant, k) for k in range(minority + 1)) / 2**discordant)
        contrasts.append(
            {
                "variant": variant,
                "paired": len(deltas),
                "planned": 3,
                "task_success_delta": sum(deltas) / len(deltas) if deltas else None,
                "exact_p": pvalue if len(deltas) == 3 else None,
                "holm_p": None,
            }
        )
    if all(item["paired"] == 3 for item in contrasts):
        for item, adjusted in zip(
            contrasts, holm_adjust([item["exact_p"] for item in contrasts]), strict=True
        ):
            item["holm_p"] = adjusted
    attempts = ledger.attempts
    known = [
        row["usage"]["total_tokens"] for row in attempts if row["usage"]["total_tokens"] is not None
    ]
    complete = (
        len(by_id) == len(TASKS)
        and set(by_id) == set(TASKS)
        and all(
            row.get("status") == "measured"
            and row.get("intervention_verified")
            and row.get("physical_accounting_verified")
            for row in records
        )
        and all(item["paired"] == 3 for item in contrasts)
        and clean
        and unchanged
        and bool(attempts)
        and all(row["state"] == "returned" and row["http_status"] == 200 for row in attempts)
    )
    return {
        "schema_version": 1,
        "live_pilot_measurement_complete": complete,
        "phase10_overall_acceptance": False,
        "main_merge_authorized": False,
        "recorded_tasks": len(records),
        "planned_tasks": len(TASKS),
        "physical_requests": {"llm": len(attempts), "tavily": 0},
        "known_vendor_tokens": sum(known),
        "vendor_tokens": sum(known) if len(known) == len(attempts) else None,
        "monetary_cost": None,
        "source_unchanged": unchanged,
        "owned_children_cleaned": clean,
        "contrasts": contrasts,
        "scenario_contracts": {
            row["task"]: (
                row.get("fault_mutations", 0) > 0
                and row.get("verification") is not None
                and row.get("task_success") is False
                and row.get("checks", {}).get("independent_checkpoint_oracle") is False
                if row["task"].startswith("deception-")
                else row.get("fault_mutations") == 1
                and row.get("task_success") is False
                and row.get("error_code") == "evaluation_replanning_disabled"
                if row["task"] == "recover-no_replanning"
                else row.get("task_success") is True
            )
            for row in records
        },
        "delta_direction": "variant_minus_baseline",
        "scope": "Real model-driven durable workflows with seeded code and closed public corpus. "
        "Three heterogeneous scenarios per variant, one attempt each, "
        "not powered general-model inference. "
        "Scripted fault injection is not scripted model output. "
        "Completion of measurement does not imply all tasks succeed.",
    }


async def run_live_ablations(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    if not args.allow_paid:
        raise ValueError(
            "Live ablations require explicit --allow-paid within an approved allowance"
        )
    revision, dirty = _git_state(repo_root)
    if not revision or dirty is not False:
        raise ValueError("Live ablations require clean committed source")
    root = (repo_root / "data/evaluation").resolve()
    if not root.is_relative_to(repo_root.resolve()):
        raise ValueError("Evaluation root escaped workspace")
    grant = approval_path(root, args.approval_id)
    version = getattr(args, "protocol_version", 1)
    if version not in (1, 2):
        raise ValueError("Unknown pilot protocol")
    prior = None
    if version == 2:
        if getattr(args, "prior_run", None) is None:
            raise ValueError("v2 must bind the original allowance and evidence")
        prior = PriorPilot(args.prior_run, root=root)
    stopped = None
    if getattr(args, "continue_v2", None) is not None:
        if prior is None:
            raise ValueError("Continuation requires protocol v2 and the original prior run")
        stopped = StoppedV2(args.continue_v2, prior)
    planned = CONTINUATION_TASKS if stopped else TASKS
    prior_count = HISTORICAL_REQUESTS if stopped else PRIOR_REQUESTS if prior else 0
    new_limit = NEW_REQUEST_LIMIT if stopped else REMAINING_REQUESTS if prior else 180
    claim = root / (CLAIM_NAME if stopped else f"live-ablation-v{version}-claim.json")
    if grant.exists() or claim.exists():
        raise ValueError("This one-shot pilot was already claimed; do not reset/replay")
    fixture, digest = load_fixture(
        repo_root / f"benchmarks/ablations/workflow_fixture_v{version}.json", WorkflowFixture
    )
    sandbox = DockerEvaluationSandbox(args.sandbox_container)
    resources = sandbox_resources(await sandbox.health(), dataset=True)
    if stopped and (
        resources != stopped.manifest["resources"] or digest != stopped.manifest["fixture_sha256"]
    ):
        raise ValueError("Continuation must retain the frozen fixture and sandbox/data resources")
    if not await no_owned_children(sandbox):
        raise ValueError("Dedicated controller already owns child resources")
    settings, _ = load_explicit_settings(repo_root / ".env", require_tavily=False)
    if prior and settings.llm_model != prior.manifest["configured_model"]:
        raise ValueError("Keep the originally configured model for this versioned pilot")
    assert settings.llm_api_key is not None
    secret = settings.llm_api_key.get_secret_value()
    prefix = (
        "live-ablation-v2-tail-" if stopped else "live-ablation-v2-" if prior else "live-ablation-"
    )
    directory = root / (prefix + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "protocol_version": version,
        "git_revision": revision,
        "tasks": list(planned),
        "fixture_sha256": digest,
        "resources": resources,
        "configured_model": settings.llm_model,
        "limits": LIMITS.model_dump(),
        "physical_limits": {
            "llm": new_limit,
            "tavily": 0,
            "per_task_llm": 12,
        },
        "max_output_tokens": OUTPUT_TOKENS if prior else 4096,
        "prior_run": prior.directory.name if prior else None,
        "prior_requests": prior_count,
        "cumulative_request_limit": 180,
        "prior_primary_sha256": stopped.hashes if stopped else prior.hashes if prior else {},
        "continuation_of": stopped.directory.name if stopped else None,
        "continuation_version": 1 if stopped else None,
        "authorized_cumulative_limit": prior_count + new_limit,
        "objective_suffix": SUFFIX if prior else OBJECTIVE_SUFFIX,
        "protocol_frozen_before_requests": True,
        "search_backend": "closed-public-engineering-corpus-v2"
        if prior
        else "closed-public-engineering-corpus",
        "automatic_task_replays": False,
    }
    _write_json(directory / "manifest.json", manifest)
    ledger: LiveAblationLedger
    if stopped:
        ledger = ContinuationLedger(
            grant, run_id=directory.name, stopped=stopped, manifest=manifest
        )
    elif prior:
        ledger = RemainingPilotLedger(grant, run_id=directory.name, prior=prior)
    else:
        with claim.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "run_id": directory.name,
                    "approval_id": args.approval_id,
                    "manifest_sha256": payload_digest(manifest),
                },
                stream,
            )
        ledger = LiveAblationLedger(grant, run_id=directory.name)
    transport = GuardedTransport(
        provider="llm",
        endpoint=settings.llm_base_url.rstrip("/") + "/chat/completions",
        ledger=ledger,
        output=directory,
        secrets=(secret,),
        max_output_tokens=OUTPUT_TOKENS if prior else 4096,
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
            for scenario in SCENARIOS:
                script = fixture.scripts[scenario].model_copy(
                    update={
                        "prompt": fixture.scripts[scenario].prompt
                        + (SUFFIX if prior else OBJECTIVE_SUFFIX)
                    }
                )
                for variant in WORKFLOW_VARIANTS:
                    task = f"{scenario}-{variant}"
                    if task not in planned:
                        continue
                    if _git_state(repo_root) != (revision, False):
                        raise ValueError("Source changed during live pilot")
                    target = directory / task
                    target.mkdir(exist_ok=False)
                    provider = (ExtractiveWorkflowProvider if prior else LiveWorkflowProvider)(
                        native, transport, task
                    )
                    record: dict[str, Any] = {
                        "task": task,
                        "scenario": scenario,
                        "variant": variant,
                        "input_sha256": payload_digest(
                            {
                                "prompt": script.prompt,
                                "specification": script.specification.model_dump(),
                                "fault": script.fault,
                                "limits": LIMITS.model_dump(),
                                "resources": resources,
                                "fixture": digest,
                            }
                        ),
                        "status": "started",
                        "task_success": None,
                    }
                    records.append(record)
                    _write_json(directory / "cases.json", records)
                    print(
                        f"Live ablation v{version} {task}; "
                        f"new requests {len(ledger.attempts)}/{ledger.limits['llm']}",
                        flush=True,
                    )
                    started = time.perf_counter()
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
                    except asyncio.CancelledError:
                        record["status"] = "cancelled"
                        raise
                    except Exception as exc:
                        record.update(status="error", error_type=type(exc).__name__)
                        raise
                    finally:
                        record["duration_seconds"] = round(time.perf_counter() - started, 3)
                        attempts = [
                            row for row in ledger.attempts if row["case_id"].startswith(task + "--")
                        ]
                        record["physical_requests"] = len(attempts)
                        usage = record.get("usage", {})
                        record["physical_accounting_verified"] = (
                            provider.calls == len(attempts) == usage.get("model_calls", 0)
                            and all(
                                row["state"] == "returned" and row["http_status"] == 200
                                for row in attempts
                            )
                            and all(
                                all(row["usage"][key] is not None for row in attempts)
                                and sum(row["usage"][key] for row in attempts) == usage.get(key, 0)
                                for key in ("input_tokens", "output_tokens", "total_tokens")
                            )
                        )
                        if (
                            prior
                            and provider.failed
                            and attempts
                            and record["physical_accounting_verified"]
                        ):
                            # An accounted unusable completion is an observed model failure.
                            # End this task, never replay it; independent scheduled tasks may run.
                            record.update(
                                status="measured",
                                task_success=False,
                                failure_class="model_protocol_failure",
                            )
                        _write_json(directory / "cases.json", records)
                    print(
                        f"  {record['status']}; task_success={record['task_success']}; "
                        f"requests={record['physical_requests']}",
                        flush=True,
                    )
                    if (
                        (
                            provider.failed
                            and record.get("failure_class") != "model_protocol_failure"
                        )
                        or record["status"] != "measured"
                        or not record["physical_accounting_verified"]
                        or not await no_owned_children(sandbox)
                    ):
                        raise ValueError(
                            "Provider/accounting/resource failure; stop without retries"
                        )
    except Exception as exc:
        print(
            f"Pilot stopped ({type(exc).__name__}); partial evidence retained; no automatic replay",
            flush=True,
        )
    finally:
        await sandbox.close()
        clean = await no_owned_children(sandbox)
        _write_json(directory / "request_ledger.json", ledger.data)
        summary = pilot_summary(
            records, ledger, clean=clean, unchanged=_git_state(repo_root) == (revision, False)
        )
        if prior:
            unchanged = stopped.unchanged() if stopped else prior.unchanged()
            summary.update(
                protocol_version=2,
                prior_evidence_unchanged=unchanged,
                prior_requests=prior_count,
                cumulative_llm_requests=prior_count + len(ledger.attempts),
                cumulative_request_limit=180,
            )
            summary["live_pilot_measurement_complete"] &= unchanged
        if stopped:
            complete = (
                [row["task"] for row in records] == list(CONTINUATION_TASKS)
                and all(
                    row.get("status") == "measured"
                    and row.get("physical_requests", 0) > 0
                    and row.get("task_success") is not None
                    and row.get("physical_accounting_verified")
                    and row.get("intervention_verified")
                    for row in records
                )
                and clean
                and summary["source_unchanged"]
                and unchanged
            )
            combined = [dict(row, source_run=stopped.directory.name) for row in stopped.records] + [
                dict(row, source_run=directory.name) for row in records
            ]
            _write_json(directory / "combined_cases.json", combined)
            combined_summary = pilot_summary(combined, ledger, clean=clean, unchanged=unchanged)
            summary.update(
                continuation_measurement_complete=bool(complete),
                continuation_planned_tasks=len(CONTINUATION_TASKS),
                cumulative_v2_recorded_tasks=len(combined),
                cumulative_v2_vendor_tokens=(
                    stopped.vendor_tokens + summary["vendor_tokens"]
                    if summary["vendor_tokens"] is not None
                    else None
                ),
                prior_interrupted_outcome_preserved=True,
                combined_contrasts=combined_summary["contrasts"],
                combined_scenario_contracts=combined_summary["scenario_contracts"],
            )
        _write_json(directory / "acceptance.json", summary)
        lines = [
            "# Live workflow ablation pilot",
            "",
            summary["scope"],
            "",
            f"Measurement complete: {summary['live_pilot_measurement_complete']}",
            f"Actual new LLM requests: {len(ledger.attempts)}/{ledger.limits['llm']}; Tavily: 0.",
            f"Protocol v{version}; earlier pilot requests: {prior_count}; cumulative limit 180.",
            "",
            "| Task | Outcome | Verified task success | LLM requests |",
            "|---|---|---|---:|",
        ]
        if stopped:
            lines += [
                "Continuation measurement complete: "
                f"{summary['continuation_measurement_complete']}",
                "Only seven previously unattempted tasks are authorized here. The original "
                "interrupted recovery No Reviewer score remains unknown; full pilot is incomplete.",
                "Combined original/new records with provenance: combined_cases.json. "
                "Code differs only by the documented terminal-error fix and continuation controls.",
                "",
            ]
        lines.extend(
            f"| {row['task']} | {row['status']} | {row.get('task_success')} | "
            f"{row.get('physical_requests')} |"
            for row in records
        )
        lines += [
            "",
            "Unattempted tasks remain in the 15-task denominator. Missing outcomes are not zero.",
            "",
            "Paired effects (variant minus baseline):",
            "",
            "```json",
            json.dumps(summary["contrasts"], indent=2),
            "```",
            "",
            "No full Phase 10 or main merge approval. Detailed inputs, responses, "
            "traces and oracle results are retained locally.",
        ]
        (directory / "evaluation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory, bool(
        summary.get("continuation_measurement_complete", summary["live_pilot_measurement_complete"])
    )
