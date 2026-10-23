"""A separately approved three-task check of Phase 10 repairs, never a grant reset."""

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.live_ablation_contract import LIMITS, LiveAblationLedger
from autoscholar.evaluation.live_ablation_contract import LiveWorkflowProvider as LiveProvider
from autoscholar.evaluation.live_ablation_v2 import OUTPUT_TOKENS, QUOTE_RULE, SUFFIX
from autoscholar.evaluation.provider_transport import Provider
from autoscholar.llm.errors import LLMResponseError
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.orchestration.models import ExperimentRetry, ReviewResult

REPAIR_TASKS = ("normal-baseline", "recover-baseline", "recover-no_replanning")
REPAIR_REQUESTS = 36
REPAIR_SCOPE = "phase10_targeted_repairs_v3"
REPAIR_CLAIM = "live-repair-v3-claim.json"
JSON_RULE = (
    " Tool arguments must be exactly one JSON object matching the supplied schema. "
    "Do not append prose, another object, repeated keys or a second copy of the payload. "
    "Research queries search external sources only: submit exactly three queries for the "
    "three requested topics. The platform separately retrieves local project documents; "
    "never plan a web query for local project files. Respect repair_constraints."
)


def repair_claim(protocol_version: int) -> str:
    if type(protocol_version) is not int or protocol_version not in (3, 4):
        raise ValueError("Only frozen repair protocols 3 and 4 are supported")
    return f"live-repair-v{protocol_version}-claim.json"


def repair_contract(protocol_version: int = 3) -> dict[str, Any]:
    repair_claim(protocol_version)
    contract = {
        "protocol_version": 3,
        "scope": REPAIR_SCOPE,
        "tasks": list(REPAIR_TASKS),
        "physical_limits": {"llm": REPAIR_REQUESTS, "tavily": 0, "per_task_llm": 12},
        "limits": LIMITS.model_dump(),
        "max_output_tokens": OUTPUT_TOKENS,
        "native_json_corrections_per_task": 1,
        "coordinator_attempts_per_stage": 2,
        "automatic_task_replays": False,
        "objective_suffix": SUFFIX,
        "json_rule": JSON_RULE,
        "quote_rule": QUOTE_RULE,
        "search_backend": "closed-public-engineering-corpus-v2",
        "acceptance": {
            "normal-baseline": "All production checks and independent oracle pass; code runs once",
            "recover-baseline": (
                "One metrics fault; experiment-only recovery; same validated source; "
                "two training runs; independent oracle passes"
            ),
            "recover-no_replanning": (
                "Reach the injected metrics fault; stop with evaluation_replanning_disabled; "
                "no replan or second training"
            ),
            "all": (
                "Known physical usage matches durable calls; source unchanged; owned children clean"
            ),
        },
        "phase10_overall_acceptance": False,
        "main_merge_authorized": False,
        "limitations": (
            "Targeted seeded-code CPU/MNIST/closed-corpus verification only. "
            "Not a complete workflow ablation, an open-corpus research benchmark, "
            "or a statistical component-benefit claim. Old results remain separate."
        ),
    }
    if protocol_version == 4:
        contract.update(
            protocol_version=4,
            scope="phase10_reviewer_retry_acceptance_v4",
            previous_grants_reusable=False,
            review_verification=(
                "Persisted positive facts; at most one correction; no automatic PASS"
            ),
            recovery_verification=(
                "Server-retained immutable DAG; ExperimentRetry only for eligible collection faults"
            ),
            runtime_schema_sha256={
                "review": payload_digest(ReviewResult.model_json_schema()),
                "experiment_retry": payload_digest(ExperimentRetry.model_json_schema()),
            },
        )
    return contract


class RepairLedger(LiveAblationLedger):
    def __init__(self, path: Path, *, run_id: str, protocol_version: int = 3) -> None:
        contract = repair_contract(protocol_version)
        self.path = path
        self.limits = {"llm": REPAIR_REQUESTS, "tavily": 0}
        self.blocked_case_ids = ()
        self.data = {
            "schema_version": 1,
            "protocol_version": protocol_version,
            "scope": contract["scope"],
            "run_id": run_id,
            "limits": self.limits,
            "tasks": list(REPAIR_TASKS),
            "per_task_llm_limit": 12,
            "automatic_retries": False,
            "native_json_corrections_per_task": 1,
            "attempts": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, allow_nan=False)

    def reserve(self, provider: Provider, case_id: str, payload: dict[str, Any]) -> int:
        if case_id.rpartition("--")[0] not in REPAIR_TASKS:
            raise ValueError("Only the three approved repair-verification tasks may dispatch")
        return super().reserve(provider, case_id, payload)


class RepairWorkflowProvider(LiveProvider):
    corrections = 0
    verify_source: Callable[[], object] | None = None

    def check_available(self) -> None:
        super().check_available()
        if self.verify_source is not None:
            self.verify_source()

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        name = tools[0].name if tools and len(tools) == 1 else "coding"
        rule = JSON_RULE + (
            QUOTE_RULE if name in {"submit_evidence", "submit_research_report"} else ""
        )
        messages = [
            replace(item, content=item.content + rule)
            if isinstance(item, ChatMessage) and item.role == "system"
            else item
            for item in messages
        ]
        if name == "submit_research_queries" and tools:
            parameters = copy.deepcopy(tools[0].parameters)
            parameters["properties"]["queries"].update(minItems=3, maxItems=3)
            tools = [replace(tools[0], parameters=parameters)]
        try:
            result = await super().generate(messages, tools=tools, tool_choice=tool_choice)
            if name == "submit_research_queries" and (
                len(result.tool_calls) != 1
                or result.tool_calls[0].name != name
                or not isinstance(result.tool_calls[0].arguments.get("queries"), list)
                or len(result.tool_calls[0].arguments["queries"]) != 3
            ):
                self.failed = True
                raise LLMResponseError(
                    code="llm_invalid_tool_call",
                    message="Exactly three external queries required",
                    usage=result.usage,
                    retryable=False,
                    model=result.model,
                    validation_reason="schema_invalid",
                )
            return result
        except LLMResponseError as exc:
            if exc.model:
                self.models.add(exc.model)
            attempts = self.transport.ledger.attempts
            known = bool(
                attempts
                and attempts[-1]["case_id"] == f"{self.task}--{self.calls}"
                and attempts[-1]["state"] == "returned"
                and attempts[-1]["http_status"] == 200
                and exc.usage is not None
                and attempts[-1]["usage"]["total_tokens"] == exc.usage.total_tokens
                and attempts[-1]["usage"]["input_tokens"] == exc.usage.input_tokens
                and attempts[-1]["usage"]["output_tokens"] == exc.usage.output_tokens
            )
            allowed = (
                known
                and exc.retryable
                and self.corrections < 1
                and self.calls < 12
                and name in {"submit_task_plan", "submit_plan_revision", "submit_review"}
                and exc.code == "llm_invalid_tool_call"
                and exc.finish_reason != "length"
            )
            if allowed:
                self.corrections += 1
                self.failed = False
            else:
                self.failed = True
            exc.retryable = bool(allowed)
            raise
