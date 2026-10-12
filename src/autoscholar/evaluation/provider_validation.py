"""Explicit bounded paid-provider capability checks, separate from engineering fixtures."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from pydantic import SecretStr

from autoscholar.agent.tools.calculator import CalculatorTool
from autoscholar.core.config import Settings
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.docker_sandbox import DockerEvaluationSandbox
from autoscholar.evaluation.isolated_components import sandbox_resources
from autoscholar.evaluation.provider_cases import (
    CASE_IDS,
    CASES,
    SYSTEM,
    grade_code,
    grade_research,
    grade_structured,
    oracle_digest,
    parse_answer,
    public_evidence,
    public_suite_digest,
)
from autoscholar.evaluation.provider_confirmation import (
    ToolConfirmationEvidence,
    load_tool_confirmation,
)
from autoscholar.evaluation.provider_prior import (
    PriorPlanning,
    followup_claim,
    load_prior,
    load_stopped_followup,
)
from autoscholar.evaluation.provider_transport import (
    LIMITS,
    MAX_OUTPUT_TOKENS,
    ApprovalLedger,
    GuardedTransport,
    Provider,
    approval_path,
    no_secrets,
)
from autoscholar.evaluation.runner import _git_state, _write_json
from autoscholar.llm.models import (
    AssistantToolCallMessage,
    ChatMessage,
    ConversationMessage,
    LLMResult,
    ToolResultMessage,
)
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from autoscholar.research.providers import TavilySearchProvider


def load_explicit_settings(path: Path) -> tuple[Settings, str]:
    # Selected fields only. Never instantiate full application .env/MCP/DB configuration.
    values = dotenv_values(path, interpolate=False)
    selected = {
        key: os.getenv(key) or values.get(key)
        for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "TAVILY_API_KEY")
    }
    if not all(selected[key] for key in ("LLM_API_KEY", "LLM_MODEL", "TAVILY_API_KEY")):
        raise ValueError("Explicit LLM/model/Tavily configuration is required")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]  # BaseSettings option, not a schema field.
        llm_base_url=selected["LLM_BASE_URL"] or "https://api.openai.com/v1",
        llm_api_key=SecretStr(selected["LLM_API_KEY"] or ""),
        llm_model=selected["LLM_MODEL"],
        llm_timeout_seconds=60,
        research_tool_backend="native",
        filesystem_tool_backend="native",
        experiment_tool_backend="native",
        git_tool_backend="disabled",
    )
    return settings, selected["TAVILY_API_KEY"] or ""


def result_payload(result: LLMResult) -> dict[str, Any]:
    # Raw reasoning is unnecessary for grading; kept only in-memory for tool round-trip.
    return {
        "text": result.text,
        "model": result.model,
        "tool_calls": [asdict(call) for call in result.tool_calls],
    }


def capability_summary(
    records: list[dict[str, Any]],
    ledger: ApprovalLedger,
    *,
    source_unchanged: bool,
    clean_resources: bool,
    state: str,
    prior: PriorPlanning | None = None,
    prior_unchanged: bool = True,
    confirmation: ToolConfirmationEvidence | None = None,
) -> dict[str, Any]:
    passed = sum(record["status"] == "passed" for record in records)
    attempts = ledger.attempts
    counts = {provider: sum(row["provider"] == provider for row in attempts) for provider in LIMITS}
    known = [
        row["usage"]["total_tokens"]
        for row in attempts
        if row["provider"] == "llm" and row["usage"]["total_tokens"] is not None
    ]
    accounted = len(known) == counts["llm"]
    accepted = (
        state == "completed"
        and tuple(row["case_id"] for row in records) == CASE_IDS
        and passed == len(CASES)
        and counts == ledger.limits
        and all(row["state"] == "returned" and row["http_status"] == 200 for row in attempts)
        and source_unchanged
        and clean_resources
        and prior_unchanged
    )
    previous = (
        confirmation.attempts
        if confirmation
        else prior.snapshots["request_ledger.json"]["attempts"]
        if prior
        else []
    )
    all_attempts = previous + attempts
    cumulative_known = [
        row["usage"]["total_tokens"]
        for row in all_attempts
        if row["provider"] == "llm" and row["usage"]["total_tokens"] is not None
    ]
    cumulative_counts = {
        provider: sum(row["provider"] == provider for row in all_attempts) for provider in LIMITS
    }
    tool_accepted = bool(
        confirmation
        and state == "completed"
        and tuple(row["case_id"] for row in records) == CASE_IDS[1:3]
        and passed == 2
        and counts == {"llm": 2, "tavily": 0}
        and all(row["state"] == "returned" and row["http_status"] == 200 for row in attempts)
        and source_unchanged
        and clean_resources
        and prior_unchanged
    )
    summary = {
        "schema_version": 1,
        "state": state,
        "planned_llm_cases": 2 if confirmation else len(CASES),
        "recorded": len(records),
        "passed": passed,
        "bounded_provider_capability_acceptance": accepted,
        "phase10_overall_acceptance": False,
        "request_limits": ledger.limits,
        "live_llm_cases_this_run": 2 if confirmation else 9 if prior else 10,
        "reused_planning_cases": int(prior is not None and confirmation is None),
        "prior_evidence": prior.binding() if prior else None,
        "prior_evidence_unchanged": prior_unchanged,
        "cumulative_physical_requests": cumulative_counts,
        "cumulative_requests_total": len(all_attempts),
        "cumulative_known_vendor_tokens": sum(cumulative_known),
        "cumulative_vendor_tokens": sum(cumulative_known)
        if len(cumulative_known) == cumulative_counts["llm"]
        else None,
        "physical_requests": counts,
        "requests_total": len(attempts),
        "known_vendor_tokens": sum(known),
        "vendor_tokens": sum(known) if accounted else None,
        "token_accounting": {"known": len(known), "requested_llm": counts["llm"]},
        "monetary_cost": None,
        "source_unchanged": source_unchanged,
        "owned_resources_cleaned": clean_resources,
        "scope": (
            "Explicit manual follow-up; one verified planning record reused with its "
            "original source identity. Initial HTTP 400 remains in prior evidence. "
            if prior
            else ""
        )
        + "Fixed public provider contracts, actual requests, no automatic retries. "
        "Production Provider/Calculator/DAG validation and isolated code oracle; NOT an autonomous "
        "LLM-driven durable E2E run, general model benchmark or statistical ablation.",
        "cases": records,
    }
    if confirmation:
        summary.update(
            tool_chain_confirmation_acceptance=tool_accepted,
            confirmation_evidence=confirmation.binding(),
            previous_case_results=confirmation.records,
            scope="Explicit two-request tool-only manual confirmation, no planning/search/other "
            "case repeat. Original failures and scores remain in bound previous evidence. "
            "A passing tool component is NOT full provider or Phase 10 acceptance.",
        )
    return summary


def write_capability_report(directory: Path, summary: dict[str, Any]) -> None:
    _write_json(directory / "provider_acceptance.json", summary)
    lines = [
        "# Phase 10 bounded real-provider verification",
        "",
        f"Bounded capability gate: **{summary['bounded_provider_capability_acceptance']}**.",
        "",
        summary["scope"],
        "",
        f"Cases: {summary['passed']} / {summary['planned_llm_cases']}; physical requests: "
        f"{summary['physical_requests']}; vendor tokens: {summary['vendor_tokens']} "
        f"(known: {summary['known_vendor_tokens']}). Monetary cost: unknown.",
        "",
        "No private design/.env content is sent. Credentials occur only in authentication headers. "
        "Missing/error/cancelled results stay in planned coverage, no paid automatic retry. "
        "This alone does not assert full Phase 10 acceptance or approve a main merge.",
        "",
    ]
    if "tool_chain_confirmation_acceptance" in summary:
        lines += [
            "",
            f"Tool-only confirmation: **{summary['tool_chain_confirmation_acceptance']}**. "
            f"Cumulative physical attempts: {summary['cumulative_requests_total']} "
            f"({summary['cumulative_physical_requests']}); cumulative known tokens: "
            f"{summary['cumulative_known_vendor_tokens']}. Original failure scores unchanged.",
        ]
    if summary["reused_planning_cases"]:
        lines += [
            "",
            "Planning was independently regraded from bound prior request/response "
            "snapshots, NOT requested again or counted as a new live response. "
            f"Prior evidence unchanged: {summary['prior_evidence_unchanged']}. "
            "Cumulative attempts including the original refusal: "
            f"{summary['cumulative_physical_requests']}; "
            f"total {summary['cumulative_requests_total']}. "
            f"Cumulative tokens: {summary['cumulative_vendor_tokens']} "
            f"(known {summary['cumulative_known_vendor_tokens']}); "
            "original unknown usage stays unknown.",
        ]
    lines += ["", "| Case | Result | Checks |", "|---|---|---|"]
    lines += [
        f"| {row['case_id']} | {row['status']} | {row.get('checks', {})} |"
        for row in summary["cases"]
    ]
    (directory / "evaluation_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def no_owned_children(sandbox: DockerEvaluationSandbox) -> bool:
    assert sandbox.context is not None
    project = sandbox.container.removesuffix("-sandbox-manager-1")
    selector = "label=autoscholar.evaluation=" + project
    for arguments in (("ps", "-aq"), ("volume", "ls", "-q")):
        if (
            await sandbox._metadata("--context", sandbox.context, *arguments, "--filter", selector)
        ).strip():
            return False
    return True


async def run_providers(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    if not args.allow_paid:
        raise ValueError("Real provider evaluation requires explicit --allow-paid approval")
    root = (repo_root / "data/evaluation").resolve()
    if not root.is_relative_to(repo_root.resolve()):
        raise ValueError("Provider output escaped evaluation root")
    grant = approval_path(root, args.approval_id)
    continuation_path = getattr(args, "continue_unattempted", None)
    confirmation_path = getattr(args, "confirm_tools_from", None)
    if confirmation_path is not None and continuation_path is not None:
        raise ValueError("Tool-only confirmation cannot continue the earlier allowance")
    if grant.exists() and continuation_path is None:
        raise ValueError("Approval already exists; no automatic rerun or allowance reset")
    revision, dirty = _git_state(repo_root)
    if not revision or dirty is not False:
        raise ValueError("Real verification requires clean committed source")
    settings, tavily_key = load_explicit_settings(repo_root / ".env")
    assert settings.llm_api_key is not None
    secrets = settings.llm_api_key.get_secret_value(), tavily_key
    endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
    url = httpx.URL(endpoint)
    if url.scheme != "https" or url.userinfo or url.query or url.fragment:
        raise ValueError("Invalid explicit HTTPS LLM endpoint")
    prior_path = getattr(args, "prior_run", None)
    prior = (
        load_prior(prior_path, repo_root=repo_root, model=settings.llm_model, endpoint=endpoint)
        if prior_path
        else None
    )
    limits: dict[Provider, int] = {"llm": 9, "tavily": 2} if prior else LIMITS
    confirmation = (
        load_tool_confirmation(
            confirmation_path,
            repo_root=repo_root,
            prior=prior,
            model=settings.llm_model,
            endpoint=endpoint,
        )
        if confirmation_path is not None and prior is not None
        else None
    )
    if confirmation_path is not None and confirmation is None:
        raise ValueError("Tool-only confirmation requires verified original evidence")
    if confirmation:
        limits = {"llm": 2, "tavily": 0}
    confirmation_claim = (
        (
            root / "provider-tool-confirmation-claims" / (confirmation.directory.name + ".json")
        ).resolve()
        if confirmation
        else None
    )
    if confirmation_claim is not None:
        if not confirmation_claim.is_relative_to(root):
            raise ValueError("Tool confirmation claim escaped the evaluation root")
        if confirmation_claim.exists():
            raise ValueError("This continuation already has a one-shot tool confirmation")
    claim = followup_claim(root, prior) if prior else None
    continuation = (
        load_stopped_followup(
            continuation_path,
            repo_root=repo_root,
            prior=prior,
            model=settings.llm_model,
            endpoint=endpoint,
            identifier=args.approval_id,
        )
        if continuation_path is not None and prior is not None
        else None
    )
    if continuation_path is not None and continuation is None:
        raise ValueError("Explicit continuation requires verified original planning evidence")
    continuation_id = continuation["directory"].name if continuation else None
    continuation_claim = (
        (root / "provider-continuation-claims" / (continuation_id + ".json")).resolve()
        if continuation_id
        else None
    )
    if continuation_claim is not None and (
        not continuation_claim.is_relative_to(root) or continuation_claim.exists()
    ):
        raise ValueError("This stopped follow-up already has an independent continuation")
    if claim is not None and claim.exists() and continuation is None and confirmation is None:
        raise ValueError("This prior run already has a follow-up; no cumulative allowance reset")
    sandbox = DockerEvaluationSandbox(args.sandbox_container)
    resources = sandbox_resources(await sandbox.health(), dataset=False)
    if not await no_owned_children(sandbox):
        raise ValueError("Dedicated controller already has running evaluation children")
    directory = root / ("provider-" + uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    if claim is not None and continuation is None and confirmation is None:
        claim.parent.mkdir(parents=True, exist_ok=True)
        with claim.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "prior_run_id": prior.directory.name if prior else None,
                    "followup_run_id": directory.name,
                    "approval_id": args.approval_id,
                },
                stream,
            )
    if continuation_claim is not None:
        continuation_claim.parent.mkdir(parents=True, exist_ok=True)
        with continuation_claim.open("x", encoding="utf-8") as stream:
            json.dump({"from_run": continuation_id, "run_id": directory.name}, stream)
    if confirmation_claim is not None:
        confirmation_claim.parent.mkdir(parents=True, exist_ok=True)
        with confirmation_claim.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "from_run": confirmation.directory.name if confirmation else None,
                    "run_id": directory.name,
                    "approval_id": args.approval_id,
                },
                stream,
            )
    ledger = (
        ApprovalLedger.continue_unused(grant, expected=continuation["ledger"])
        if continuation
        else ApprovalLedger(
            grant,
            run_id=directory.name,
            limits=limits,
            blocked_case_ids=tuple(case.id for case in CASES if case not in CASES[1:3])
            if confirmation
            else ("planning",)
            if prior
            else (),
        )
    )
    llm_transport = GuardedTransport(
        provider="llm", endpoint=endpoint, ledger=ledger, output=directory, secrets=secrets
    )
    search_transport = GuardedTransport(
        provider="tavily",
        endpoint="https://api.tavily.com/search",
        ledger=ledger,
        output=directory,
        secrets=secrets,
    )
    records: list[dict[str, Any]] = (
        []
        if confirmation
        else continuation["records"]
        if continuation
        else ([prior.record] if prior else [])
    )
    if continuation:
        # No inferred/fabricated native result; its dependent contract stays unexecuted.
        records.append(
            {
                "case_id": CASES[2].id,
                "kind": CASES[2].kind,
                "status": "skipped",
                "checks": {},
                "reason": "native_tool_response_not_available",
                "input_sha256": payload_digest(asdict(CASES[2])),
                "duration_ms": 0.0,
            }
        )
    state, clean = "error", False
    manifest = {
        "schema_version": 1,
        "run_id": directory.name,
        "git_revision": revision,
        "git_dirty": False,
        "suite_sha256": public_suite_digest(),
        "oracle_sha256": oracle_digest(),
        "sandbox_resources": resources,
        "configured_model": settings.llm_model,
        "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "request_limits": limits,
        "prior_evidence": prior.binding() if prior else None,
        "continued_from": continuation_id,
        "tool_confirmation_of": confirmation.binding() if confirmation else None,
        "max_tokens_per_request": MAX_OUTPUT_TOKENS,
        "started_at": datetime.now(UTC).isoformat(),
        "approval_id": args.approval_id,
    }
    no_secrets(manifest, secrets)
    _write_json(directory / "manifest.json", manifest)
    if prior and not confirmation:
        _write_json(
            directory / "planning_evidence.json",
            {"binding": prior.binding(), "record": prior.record},
        )
        _write_json(directory / "cases.json", records)
    tool_result: LLMResult | None = None
    tool_output: str | None = None
    try:
        async with (
            httpx.AsyncClient(
                transport=llm_transport, follow_redirects=False, trust_env=False, timeout=60
            ) as llm_client,
            httpx.AsyncClient(
                transport=search_transport, follow_redirects=False, trust_env=False, timeout=30
            ) as search_client,
        ):
            provider = OpenAICompatibleProvider(settings, http_client=llm_client)
            search = TavilySearchProvider(api_key=tavily_key, client=search_client, retry_delays=())
            selected_cases = (
                CASES[1:3]
                if confirmation
                else CASES[3:]
                if continuation
                else CASES[1:]
                if prior
                else CASES
            )
            for case in selected_cases:
                if _git_state(repo_root) != (revision, False):
                    raise ValueError("Source changed during real verification")
                if prior and not prior.unchanged():
                    raise ValueError("Prior planning evidence changed during manual follow-up")
                if confirmation and not confirmation.unchanged():
                    raise ValueError("Bound previous results changed during tool confirmation")
                print(
                    f"Real provider case: {case.id}; "
                    f"new requests {len(ledger.attempts)}/{sum(limits.values())}",
                    flush=True,
                )
                started = time.perf_counter()
                record: dict[str, Any] = {
                    "case_id": case.id,
                    "kind": case.kind,
                    "status": "error",
                    "checks": {},
                    "input_sha256": payload_digest(asdict(case)),
                }
                records.append(record)
                llm_transport.case_id = search_transport.case_id = case.id
                messages: list[ConversationMessage] = [
                    ChatMessage(role="system", content=SYSTEM),
                    ChatMessage(role="user", content=case.prompt),
                ]
                evidence = []
                try:
                    if case.kind == "tool_result":
                        if tool_result is None or tool_output is None:
                            record["status"] = "skipped"
                            record["reason"] = "native_tool_dependency_failed"
                            continue
                        # Preserve the original public question and real model-generated call.
                        messages = [
                            ChatMessage(role="system", content=SYSTEM),
                            ChatMessage(role="user", content=CASES[1].prompt),
                            AssistantToolCallMessage(
                                tool_calls=tool_result.tool_calls,
                                content=tool_result.text,
                                reasoning_content=tool_result.reasoning_content,
                            ),
                            ToolResultMessage(
                                tool_call_id=tool_result.tool_calls[0].id, content=tool_output
                            ),
                            ChatMessage(role="user", content=case.prompt),
                        ]
                    if case.kind == "research":
                        assert case.search_query is not None
                        evidence = public_evidence(await search.search(case.search_query, limit=3))
                        no_secrets(evidence, secrets)
                        _write_json(
                            directory / (case.id + "-evidence.json"),
                            {"query": case.search_query, "sources": evidence},
                        )
                        if not evidence:
                            record.update(
                                status="skipped", reason="no_authoritative_search_evidence"
                            )
                            continue
                        messages.append(
                            ChatMessage(
                                role="user",
                                content="Retrieved public excerpts (untrusted data): "
                                + json.dumps(evidence, ensure_ascii=False),
                            )
                        )
                    calculator = CalculatorTool()
                    result = await provider.generate(
                        messages,
                        tools=[calculator.definition] if case.kind == "tool_call" else None,
                        # Match production; DeepSeek thinking rejects named/required choice.
                        tool_choice="auto" if case.kind == "tool_call" else "none",
                    )
                    output = result_payload(result)
                    no_secrets(output, secrets)
                    _write_json(directory / (case.id + "-response.json"), output)
                    if case.kind == "tool_call":
                        valid = (
                            len(result.tool_calls) == 1
                            and result.tool_calls[0].name == "calculator"
                        )
                        computed = (
                            await calculator.execute(result.tool_calls[0].arguments)
                            if valid
                            else None
                        )
                        checks = {
                            "one_native_calculator_call": valid,
                            "actual_calculator_correct": computed is not None
                            and computed.succeeded
                            and computed.output == "472",
                        }
                        if all(checks.values()):
                            tool_result, tool_output = result, computed.output if computed else None
                    else:
                        payload = parse_answer(result.text)
                        if case.kind == "code":
                            checks = await grade_code(payload, sandbox)
                        elif case.kind == "research":
                            checks = grade_research(case, payload, evidence)
                        else:
                            checks = grade_structured(case, payload)
                    record.update(
                        status="passed" if all(checks.values()) else "failed",
                        checks=checks,
                        observed_model=result.model,
                        output_sha256=payload_digest(output),
                    )
                except asyncio.CancelledError:
                    record["status"] = "cancelled"
                    state = "cancelled"
                    raise
                except ValueError:
                    record.update(status="failed", reason="bounded_contract_or_parse_failure")
                finally:
                    record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    _write_json(directory / "cases.json", records)
            state = "completed"
    except asyncio.CancelledError:
        state = "cancelled"
        raise
    except Exception as exc:
        if records and records[-1]["status"] == "error":
            records[-1]["reason"] = "provider_or_infrastructure_failure"
            records[-1]["error_type"] = type(exc).__name__
        # SDK exceptions/chained server bodies may contain URLs or credentials.
        print(
            f"Real verification stopped; partial evidence: {directory}; no paid retry", flush=True
        )
    finally:
        await sandbox.close()
        clean = await no_owned_children(sandbox)
        manifest.update(state=state, completed_at=datetime.now(UTC).isoformat())
        _write_json(directory / "manifest.json", manifest)
        _write_json(directory / "request_ledger.json", ledger.data)
        summary = capability_summary(
            records,
            ledger,
            source_unchanged=_git_state(repo_root) == (revision, False),
            clean_resources=clean,
            state=state,
            prior=prior,
            prior_unchanged=confirmation.unchanged()
            if confirmation
            else prior.unchanged()
            if prior
            else True,
            confirmation=confirmation,
        )
        if continuation:
            summary["continued_from"] = continuation_id
            summary["new_requests_in_continuation"] = len(ledger.attempts) - 1
        write_capability_report(directory, summary)
    return directory, bool(
        summary.get(
            "tool_chain_confirmation_acceptance", summary["bounded_provider_capability_acceptance"]
        )
    )
