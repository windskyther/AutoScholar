"""Frozen small-sample live workflow protocol; no implicit paid retries or search HTTP."""

import json
import re
from pathlib import Path
from typing import Any

from autoscholar.core.budget import BudgetLimits
from autoscholar.evaluation.datasets import decode_json
from autoscholar.evaluation.provider_transport import ApprovalLedger, GuardedTransport, Provider
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS
from autoscholar.evaluation.workflow_fixture import WorkflowFixture
from autoscholar.llm.base import LLMProvider
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.research.models import SearchResponse, SearchResult, SourceType

SCENARIOS = ("normal", "recover", "deception")
TASKS = tuple(f"{scenario}-{variant}" for scenario in SCENARIOS for variant in WORKFLOW_VARIANTS)
PER_TASK_REQUESTS = 12
TOTAL_REQUESTS = 180
LIMITS = BudgetLimits(
    steps=5,
    replans=1,
    model_calls=12,
    tool_calls=30,
    search_queries=6,
    code_repairs=0,
    training_runs=2,
    sandbox_runs=12,
    total_tokens=120000,
    wall_seconds=600,
)
OBJECTIVE_SUFFIX = (
    " Use exactly three DAG steps: research (research), code (coding, depends on research), "
    "train (experiment, depends on code). Use the fixed specification unchanged. "
    "Research uses a closed public engineering corpus, not fresh internet results; "
    "use three distinct searches about MNIST subset scope, sandbox isolation and recovery. "
    "Quote selected source sentences verbatim as evidence claims, cite each selected source "
    "once and do not invent outside facts. Coding inspects the supplied working templates."
)


class LiveAblationLedger(ApprovalLedger):
    """Separate grant, fixed 15 tasks x 12 requests; legacy grant ceilings stay untouched."""

    def __init__(self, path: Path, *, run_id: str) -> None:
        self.path = path
        self.limits = {"llm": TOTAL_REQUESTS, "tavily": 0}
        self.blocked_case_ids = ()
        self.data = {
            "schema_version": 1,
            "run_id": run_id,
            "limits": self.limits,
            "tasks": list(TASKS),
            "per_task_llm_limit": PER_TASK_REQUESTS,
            "attempts": [],
            "automatic_retries": False,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(self.data, stream, allow_nan=False)

    def reserve(self, provider: Provider, case_id: str, payload: dict[str, Any]) -> int:
        task, separator, ordinal = case_id.rpartition("--")
        if provider != "llm" or not separator or task not in TASKS:
            raise ValueError("Only the frozen live LLM task schedule is authorized")
        used = sum(row["case_id"].startswith(task + "--") for row in self.attempts)
        if ordinal != str(used + 1) or used >= PER_TASK_REQUESTS:
            raise ValueError("Per-task physical request limit or sequence violated")
        return super().reserve(provider, case_id, payload)


class LiveWorkflowProvider:
    """Observe genuine production calls; never supply scripted decisions or fake usage."""

    def __init__(self, provider: LLMProvider, transport: GuardedTransport, task: str) -> None:
        if task not in TASKS:
            raise ValueError("Unknown live workflow task")
        self.provider, self.transport, self.task = provider, transport, task
        self.calls = 0
        self.stage_calls: dict[str, int] = {}
        self.memory_payloads = 0
        self.models: set[str] = set()
        self.failed = False

    @property
    def configured(self) -> bool:
        return self.provider.configured

    async def close(self) -> None:
        pass  # Shared provider belongs to the run, not an individual task.

    def check_available(self) -> None:
        if self.failed or self.calls >= PER_TASK_REQUESTS:
            raise ValueError("Live task stopped; no retry/reset of its request allowance")

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        self.check_available()
        self.calls += 1
        self.transport.case_id = f"{self.task}--{self.calls}"
        names = [tool.name for tool in tools or []]
        stage = (
            names[0] if len(names) == 1 else "coding" if "submit_code_ready" in names else "other"
        )
        self.stage_calls[stage] = self.stage_calls.get(stage, 0) + 1
        if stage in ("submit_task_plan", "submit_plan_revision"):
            for message in messages:
                if isinstance(message, ChatMessage) and message.role == "user":
                    try:
                        payload = decode_json(message.content.encode())
                    except ValueError:
                        continue
                    if isinstance(payload, dict) and payload.get("memory_context_untrusted"):
                        self.memory_payloads += 1
                        break
        try:
            result = await self.provider.generate(messages, tools=tools, tool_choice=tool_choice)
            if result.usage is None:
                self.failed = True
            self.models.add(result.model)
            return result
        except BaseException:
            self.failed = True
            raise


class ClosedCorpusSearch:
    """Lexical ranking of self-authored public scope statements, not a Tavily mock."""

    name = "closed-public-engineering-corpus"
    source_type: SourceType = "web"
    configured = True

    def __init__(self, fixture: WorkflowFixture) -> None:
        self.fixture = fixture
        self.calls = 0
        self.records: list[dict[str, Any]] = []

    async def close(self) -> None:
        pass

    async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        if not query.strip() or self.calls >= 6:
            raise ValueError("Closed corpus search bound exceeded")
        self.calls += 1
        words = set(re.findall(r"\w+", query.lower()))
        ranked = sorted(
            self.fixture.sources.items(),
            key=lambda item: (
                -len(words & set(re.findall(r"\w+", item[0] + " " + item[1].content.lower()))),
                item[0],
            ),
        )
        response = SearchResponse(
            provider=self.name,
            source_type="web",
            query=query,
            results=tuple(
                SearchResult(
                    source_type="web",
                    provider=self.name,
                    title="Public engineering scope: " + topic,
                    url=f"https://example.org/autoscholar-public/{topic}",
                    content=source.content,
                    external_id=topic,
                )
                for topic, source in ranked[: max(0, min(limit, 1))]
            ),
        )
        self.records.append(response.to_dict())
        return response
