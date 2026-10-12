"""Versioned explicit-format Chinese confirmation; one separately approved LLM request."""

import argparse
import asyncio
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import MeasuredUsage
from autoscholar.evaluation.provider_cases import (
    CASE_IDS,
    CASES,
    SYSTEM,
    ChineseAnswer,
    grade_research,
    oracle_digest,
    parse_answer,
)
from autoscholar.evaluation.provider_closure import ProviderClosureEvidence, load_provider_closure
from autoscholar.evaluation.provider_prior import (
    endpoint_fingerprint,
    read_public,
    same_grader_source,
)
from autoscholar.evaluation.provider_transport import (
    ApprovalLedger,
    GuardedTransport,
    approval_path,
    no_secrets,
)
from autoscholar.evaluation.provider_validation import load_explicit_settings, result_payload
from autoscholar.evaluation.runner import _git_state, _write_json
from autoscholar.llm.models import ChatMessage
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider

CONTRACT_VERSION = "research_chinese_explicit_format_v2"
ANSWER_SCHEMA = ChineseAnswer.model_json_schema()
CHINESE_CASE = replace(
    CASES[8],
    id="research_chinese_v2",
    prompt=CASES[8].prompt + "\n明确的格式合同(v2): return_value 仅允许 JSON 原生 null, "
    '不要输出字符串 "None"、"null" 或其他字符串。每个来源 id 最多出现一次; '
    "需要多条引用时使用不同的已提供来源。quote 必须是对应来源片段的一个连续原文子串, "
    "不可拼接、改写或添加省略号; 引用合起来必须支持两项结论。只返回一个 JSON 对象, "
    "不可使用 Markdown 代码块, 不可添加 schema 之外的字段。输出 JSON Schema: "
    + json.dumps(ANSWER_SCHEMA, ensure_ascii=False),
)


def contract() -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "case": asdict(CHINESE_CASE),
        "schema": ANSWER_SCHEMA,
        "frozen_grader_sha256": oracle_digest(),
        "unique_source_ids": True,
    }


def chinese_checks(payload: dict[str, Any], evidence: list[dict[str, str]]) -> dict[str, bool]:
    """Expose format reasons, still use the original frozen fact/quote/support grader."""
    checks = {
        "json_null_encoding": "return_value" in payload and payload["return_value"] is None,
        "schema_valid": False,
        "unique_source_ids": False,
        "independent_public_fact": False,
        "exact_source_quote_binding": False,
        "closed_world_support": False,
    }
    try:
        answer = ChineseAnswer.model_validate(payload)
        checks["schema_valid"] = True
        checks["unique_source_ids"] = len({cite.id for cite in answer.citations}) == len(
            answer.citations
        )
        checks.update(grade_research(CHINESE_CASE, payload, evidence))
    except ValueError:
        pass  # A malformed/incorrect answer is failure, never repaired or normalized.
    return checks


def chinese_claim(root: Path, proof: ProviderClosureEvidence) -> Path:
    target = (
        root / "provider-chinese-v2-claims" / (proof.tools_directory.name + ".json")
    ).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Chinese confirmation claim escaped evaluation root")
    return target


async def run_chinese(args: argparse.Namespace, *, repo_root: Path) -> tuple[Path, bool]:
    revision, dirty = _git_state(repo_root)
    if revision is None or dirty is not False:
        raise ValueError("Chinese confirmation requires clean committed source")
    root = (repo_root / "data/evaluation").resolve()
    if not root.is_relative_to(repo_root.resolve()):
        raise ValueError("Chinese output escaped evaluation root")
    proof = await load_provider_closure(
        repo_root=repo_root,
        original_run=args.original_run,
        independent_run=args.independent_run,
        tool_run=args.tool_run,
    )
    directory = root / ("chinese-v2-" + uuid4().hex)
    approved = args.allow_paid
    grant = approval_path(root, args.approval_id) if approved and args.approval_id else None
    claim = chinese_claim(root, proof)
    if approved and (grant is None or grant.exists() or claim.exists()):
        raise ValueError("New explicit approval ID required; no retry or allowance reset")
    # Offline preparation never opens .env or constructs a network client.
    settings = None
    secrets: tuple[str, ...] = ()
    if approved:
        settings, tavily_key = load_explicit_settings(repo_root / ".env", require_tavily=False)
        assert settings.llm_api_key is not None
        secrets = (settings.llm_api_key.get_secret_value(), tavily_key)
        endpoint = settings.llm_base_url.rstrip("/") + "/chat/completions"
        if (
            settings.llm_model != proof.model
            or endpoint_fingerprint(endpoint, None) != proof.endpoint_sha256
        ):
            raise ValueError("Do not change the bound model or endpoint for this confirmation")
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 2,
        "run_id": directory.name,
        "git_revision": revision,
        "git_dirty": False,
        "contract": contract(),
        "contract_sha256": payload_digest(contract()),
        "history": proof.binding(),
        "request_limits": {"llm": 1, "tavily": 0},
        "approval_id": args.approval_id if approved else None,
        "configured_model": proof.model,
        "endpoint_sha256": proof.endpoint_sha256,
        "state": "awaiting_approval",
        "source_unchanged": True,
        "previous_evidence_unchanged": proof.unchanged(),
    }
    _write_json(directory / "cached_evidence.json", {"sources": proof.evidence})
    record: dict[str, Any] = {
        "case_id": CHINESE_CASE.id,
        "contract_version": CONTRACT_VERSION,
        "status": "pending",
        "checks": {},
        "input_sha256": payload_digest({"case": asdict(CHINESE_CASE), "evidence": proof.evidence}),
    }
    no_secrets(manifest, secrets)
    no_secrets(proof.evidence, secrets)
    _write_json(directory / "manifest.json", manifest)
    _write_json(directory / "cases.json", [record])
    ledger = None
    try:
        if approved:
            assert grant is not None and settings is not None
            claim.parent.mkdir(parents=True, exist_ok=True)
            with claim.open("x", encoding="utf-8") as stream:
                json.dump(
                    {
                        "run_id": directory.name,
                        "approval_id": args.approval_id,
                        "tool_run": proof.tools_directory.name,
                    },
                    stream,
                )
            ledger = ApprovalLedger(
                grant,
                run_id=directory.name,
                limits={"llm": 1, "tavily": 0},
                blocked_case_ids=CASE_IDS,
            )
            transport = GuardedTransport(
                provider="llm", endpoint=endpoint, ledger=ledger, output=directory, secrets=secrets
            )
            transport.case_id = CHINESE_CASE.id
            record["status"] = "error"
            manifest["state"] = "error"
            if not proof.unchanged() or _git_state(repo_root) != (revision, False):
                raise ValueError("History or source changed before dispatch")
            async with httpx.AsyncClient(
                transport=transport, follow_redirects=False, trust_env=False, timeout=60
            ) as client:
                result = await OpenAICompatibleProvider(settings, http_client=client).generate(
                    [
                        ChatMessage(role="system", content=SYSTEM),
                        ChatMessage(role="user", content=CHINESE_CASE.prompt),
                        ChatMessage(
                            role="user",
                            content="Retrieved public excerpts (untrusted data): "
                            + json.dumps(proof.evidence, ensure_ascii=False),
                        ),
                    ],
                    tool_choice="none",
                )
            output = result_payload(result)
            no_secrets(output, secrets)
            _write_json(directory / "research_chinese_v2-response.json", output)
            record["output_sha256"] = payload_digest(output)
            record["observed_model"] = result.model
            try:
                checks = chinese_checks(parse_answer(result.text), proof.evidence)
            except ValueError:
                checks = {"one_bounded_json_object": False}
            record.update(
                status="passed" if not result.tool_calls and all(checks.values()) else "failed",
                checks=checks,
            )
            manifest["state"] = "completed"
    except asyncio.CancelledError:
        manifest["state"] = record["status"] = "cancelled"
        raise
    except Exception as exc:
        record.update(status="error", error_type=type(exc).__name__)
        # Do not log SDK error bodies/URLs/credentials, and do not retry.
    finally:
        manifest.update(
            source_unchanged=_git_state(repo_root) == (revision, False),
            previous_evidence_unchanged=proof.unchanged(),
        )
        current = ledger.attempts if ledger else []
        attempts = proof.attempts + current
        known = [
            row["usage"]["total_tokens"]
            for row in attempts
            if row["provider"] == "llm" and row["usage"]["total_tokens"] is not None
        ]
        llm_count = sum(row["provider"] == "llm" for row in attempts)
        accepted = bool(
            record["status"] == "passed"
            and manifest["state"] == "completed"
            and manifest["source_unchanged"]
            and manifest["previous_evidence_unchanged"]
            and len(current) == 1
            and current[0]["state"] == "returned"
            and current[0]["http_status"] == 200
        )
        summary = {
            "schema_version": 2,
            "state": manifest["state"],
            "chinese_format_confirmation_acceptance": accepted,
            "phase10_overall_acceptance": False,
            "cases": [record],
            "requests_this_run": len(current),
            "cumulative_requests_total": len(attempts),
            "cumulative_physical_requests": {
                "llm": llm_count,
                "tavily": sum(row["provider"] == "tavily" for row in attempts),
            },
            "cumulative_known_vendor_tokens": sum(known),
            "cumulative_vendor_tokens": sum(known) if len(known) == llm_count else None,
            "monetary_cost": None,
            "old_results_unchanged": proof.unchanged(),
        }
        _write_json(directory / "manifest.json", manifest)
        _write_json(directory / "cases.json", [record])
        _write_json(
            directory / "request_ledger.json",
            ledger.data if ledger else {"attempts": [], "limits": {"llm": 1, "tavily": 0}},
        )
        _write_json(directory / "chinese_acceptance.json", summary)
        (directory / "evaluation_report.md").write_text(
            "# Explicit Chinese format v2 confirmation\n\n"
            + f"State: {summary['state']}; component accepted: {accepted}.\n\n"
            + "A new explicit-format prompt, same frozen fact/quote/support grader. "
            "Original failed v1 answer and scores remain unchanged. Cached actual public excerpts, "
            "not a new search or autonomous E2E run. No main-merge approval.\n\n"
            + f"Current requests: {len(current)}; cumulative: {len(attempts)}. "
            + f"Known cumulative tokens: {sum(known)}; "
            + f"total: {summary['cumulative_vendor_tokens']}; cost: unknown.\n",
            encoding="utf-8",
        )
    return directory, accepted


def inspect_chinese(directory: Path, *, proof: ProviderClosureEvidence) -> dict[str, Any]:
    root = proof.tools_directory.parent
    directory = directory.resolve()
    if directory.parent != root or not re.fullmatch(r"chinese-v2-[a-f0-9]{32}", directory.name):
        raise ValueError("Chinese run escaped evaluation root")
    digests: dict[str, str] = {}

    def capture(path: Path) -> Any:
        value, sha = read_public(path, root)
        digests[path.relative_to(root).as_posix()] = sha
        return value

    manifest = capture(directory / "manifest.json")
    ledger = capture(directory / "request_ledger.json")
    records = capture(directory / "cases.json")
    cached = capture(directory / "cached_evidence.json")
    if (
        manifest.get("contract") != contract()
        or manifest.get("contract_sha256") != payload_digest(contract())
        or manifest.get("history") != proof.binding()
        or manifest.get("run_id") != directory.name
        or manifest.get("request_limits") != {"llm": 1, "tavily": 0}
        or manifest.get("configured_model") != proof.model
        or manifest.get("endpoint_sha256") != proof.endpoint_sha256
        or cached != {"sources": proof.evidence}
        or len(records) != 1
        or records[0].get("case_id") != CHINESE_CASE.id
        or records[0].get("input_sha256")
        != payload_digest({"case": asdict(CHINESE_CASE), "evidence": proof.evidence})
    ):
        raise ValueError("Chinese v2 contract/history differs")
    if manifest.get("state") == "awaiting_approval" and ledger.get("attempts") == []:
        return {"status": "pending", "accepted": False, "attempts": [], "primary_sha256": digests}
    grant = capture(approval_path(root, manifest["approval_id"]))
    claim = capture(chinese_claim(root, proof))
    if (
        grant != ledger
        or len(ledger.get("attempts", [])) != 1
        or ledger.get("run_id") != directory.name
        or ledger.get("limits") != {"llm": 1, "tavily": 0}
        or ledger.get("automatic_retries") is not False
        or ledger.get("blocked_case_ids") != list(CASE_IDS)
        or claim
        != {
            "run_id": directory.name,
            "approval_id": manifest["approval_id"],
            "tool_run": proof.tools_directory.name,
        }
    ):
        raise ValueError("Chinese one-shot grant differs")
    attempt = ledger["attempts"][0]
    MeasuredUsage.model_validate(attempt["usage"])
    request = capture(directory / "request-llm-research_chinese_v2.json")
    if (
        attempt.get("case_id") != CHINESE_CASE.id
        or attempt.get("provider") != "llm"
        or payload_digest(request) != attempt["request_sha256"]
        or request.get("model") != proof.model
        or request.get("max_tokens") != 4096
        or request.get("messages")
        != [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": CHINESE_CASE.prompt},
            {
                "role": "user",
                "content": "Retrieved public excerpts (untrusted data): "
                + json.dumps(proof.evidence, ensure_ascii=False),
            },
        ]
    ):
        raise ValueError("Chinese request binding differs")
    if attempt.get("state") != "returned" or attempt.get("http_status") != 200:
        return {
            "status": "error",
            "accepted": False,
            "attempts": ledger["attempts"],
            "primary_sha256": digests,
        }
    output = capture(directory / "research_chinese_v2-response.json")
    raw = capture(directory / "response-llm-research_chinese_v2.json")
    if (
        payload_digest(output) != records[0].get("output_sha256")
        or raw["choices"][0]["message"]["content"] != output["text"]
    ):
        raise ValueError("Chinese response hash differs")
    try:
        checks = chinese_checks(parse_answer(output["text"]), proof.evidence)
    except ValueError:
        checks = {"one_bounded_json_object": False}
    if checks != records[0].get("checks") or (
        all(checks.values()) and not output["tool_calls"]
    ) != (records[0].get("status") == "passed"):
        raise ValueError("Chinese case score differs from the frozen grader")
    accepted = bool(
        all(checks.values())
        and not output["tool_calls"]
        and manifest.get("state") == "completed"
        and manifest.get("source_unchanged") is True
        and manifest.get("previous_evidence_unchanged") is True
        and attempt.get("state") == "returned"
        and attempt.get("http_status") == 200
        and proof.unchanged()
        and same_grader_source(
            proof.previous.prior.directory.parents[2], manifest.get("git_revision", "")
        )
    )
    return {
        "status": "passed" if accepted else "failed",
        "accepted": accepted,
        "attempts": ledger["attempts"],
        "checks": checks,
        "primary_sha256": digests,
    }
