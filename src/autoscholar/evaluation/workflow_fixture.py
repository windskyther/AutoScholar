"""Bounded public workflow scripts, never gold labels, real providers or settings."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from autoscholar.coding.sandbox import (
    SandboxArtifact,
    SandboxExecutor,
    SandboxHealth,
    SandboxRunRequest,
    SandboxRunResult,
)
from autoscholar.core.budget import BudgetLimits
from autoscholar.evaluation.component_fixture import VersionedFixture
from autoscholar.evaluation.datasets import decode_json
from autoscholar.evaluation.experiment_adapter import BoundedExperimentSpecification
from autoscholar.evaluation.models import EvaluationModel, Identifier
from autoscholar.evaluation.research_adapter import TOPICS, PublicSource
from autoscholar.llm.models import (
    ChatMessage,
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.orchestration.models import PlanStep, TaskPlan
from autoscholar.research.models import SearchResponse, SearchResult, SourceType


class WorkflowLimits(BudgetLimits):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    steps: int = Field(default=5, ge=1, le=6)
    replans: int = Field(default=1, ge=0, le=1)
    model_calls: int = Field(default=24, ge=1, le=40)
    tool_calls: int = Field(default=30, ge=1, le=50)
    search_queries: int = Field(default=3, ge=1, le=6)
    code_repairs: int = Field(default=0, ge=0, le=1)
    training_runs: int = Field(default=2, ge=1, le=2)
    sandbox_runs: int = Field(default=12, ge=1, le=16)
    total_tokens: int = Field(default=120, ge=1, le=1000)
    wall_seconds: int = Field(default=300, ge=1, le=600)


class WorkflowScript(EvaluationModel):
    prompt: str = Field(min_length=1, max_length=10000)
    specification: BoundedExperimentSpecification
    limits: WorkflowLimits = Field(default_factory=WorkflowLimits)
    fault: Literal["none", "invalid_metrics_once", "tampered_accuracy"] = "none"
    restart_after_coding: bool = False


class WorkflowFixture(VersionedFixture):
    sources: dict[Identifier, PublicSource] = Field(min_length=3, max_length=3)
    scripts: dict[Identifier, WorkflowScript] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def source_topics(self) -> "WorkflowFixture":
        if set(self.sources) != set(TOPICS) or any(
            identifier != source.topic for identifier, source in self.sources.items()
        ):
            raise ValueError("Workflow fixture needs one source per fixed public topic")
        return self


def workflow_plan(script: WorkflowScript) -> TaskPlan:
    return TaskPlan(
        goal=script.prompt,
        steps=[
            PlanStep(
                id="research",
                type="research",
                description="Research the public scope and cite sources",
                expected_output="Three bound public evidence records",
            ),
            PlanStep(
                id="code",
                type="coding",
                description="Inspect and validate the seeded MNIST code",
                dependencies=["research"],
                expected_output="Validated train.py and test_models.py",
            ),
            PlanStep(
                id="train",
                type="experiment",
                description="Run the fixed CPU MLP/CNN comparison",
                dependencies=["code"],
                expected_output="Measured metrics, plots, checkpoints and report",
            ),
        ],
    )


class ScriptedWorkflowProvider:
    def __init__(
        self, fixture: WorkflowFixture, script: WorkflowScript, *, project_source: str | None = None
    ) -> None:
        self.fixture = fixture
        self.script = script
        self.calls = 0
        self.stage_calls: dict[str, int] = {}
        self.memory_payloads = 0
        self.project_source = project_source

    @property
    def configured(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        if not tools or self.calls >= 40:
            raise ValueError("Unexpected or unbounded scripted workflow call")
        self.calls += 1
        names = {tool.name for tool in tools}
        name = tools[0].name
        arguments: dict[str, Any]
        if name == "submit_task_plan":
            arguments = workflow_plan(self.script).model_dump()
        elif name == "submit_plan_revision":
            arguments = {
                "plan": workflow_plan(self.script).model_dump(),
                "rerun_steps": ["train"],
                "reason": "Retry the failed experiment only; preserve validated research and code",
            }
        elif name == "submit_review":
            # Deliberately PASS even on invalid artifacts: deterministic rules must override it.
            arguments = {"status": "PASS", "issues": [], "suggested_steps": []}
        elif name == "submit_plan":
            arguments = {
                "mode": "research",
                "steps": ["Search three public topics", "Cite evidence"],
            }
        elif name == "submit_research_queries":
            arguments = {
                "queries": [
                    {
                        "topic": topic,
                        "query": topic,
                        "source_type": "web",
                        "purpose": "Public engineering scope",
                    }
                    for topic in TOPICS
                ]
            }
        elif name == "submit_evidence":
            arguments = {
                "items": [
                    {
                        "candidate_id": f"C{index}",
                        "claim": self.fixture.sources[topic].content,
                        "relevance": 1.0,
                    }
                    for index, topic in enumerate(TOPICS, 1)
                ]
            }
            if self.project_source:
                arguments["items"].append(
                    {"candidate_id": "C4", "claim": self.project_source, "relevance": 1.0}
                )
        elif name == "submit_research_report":
            arguments = {
                "answer": "\n".join(
                    f"{self.fixture.sources[topic].content} [E{index}]"
                    for index, topic in enumerate(TOPICS, 1)
                ),
                "citations": [
                    {"claim": self.fixture.sources[topic].content, "evidence_ids": [f"E{index}"]}
                    for index, topic in enumerate(TOPICS, 1)
                ],
            }
            if self.project_source:
                arguments["answer"] += f"\n{self.project_source} [E4]"
                arguments["citations"].append(
                    {"claim": self.project_source, "evidence_ids": ["E4"]}
                )
        elif "submit_code_ready" in names:
            # Snapshot mode supplies complete sources and intentionally removes read/list tools.
            user = messages[-1]
            if not isinstance(user, ChatMessage) or user.role != "user":
                raise ValueError("Expected the real coding source snapshot")
            snapshot = decode_json(user.content.encode())
            if not isinstance(snapshot, dict):
                raise ValueError("Invalid coding snapshot")
            sources = snapshot.get("source_files")
            if (
                not isinstance(sources, dict)
                or not {"train.py", "test_models.py"} <= sources.keys()
            ):
                raise ValueError("Seeded coding source is incomplete")
            name, arguments = (
                "submit_code_ready",
                {"summary": "Inspected full seeded source snapshot; run mandatory validation"},
            )
        else:
            raise ValueError("Unexpected scripted workflow stage")
        if name not in names:
            raise ValueError("Scripted workflow requested an unavailable tool")
        self.stage_calls[name] = self.stage_calls.get(name, 0) + 1
        if name in {"submit_task_plan", "submit_plan_revision"}:
            user = messages[-1]
            if not isinstance(user, ChatMessage):
                raise ValueError("Missing actual planning payload")
            payload = decode_json(user.content.encode())
            if not isinstance(payload, dict):
                raise ValueError("Invalid actual planning payload")
            self.memory_payloads += bool(payload.get("memory_context_untrusted"))
        return LLMResult(
            text="",
            model="scripted-public-workflow",
            usage=TokenUsage(2, 1, 3),
            tool_calls=(
                ToolCall(id=f"public-workflow-{self.calls}", name=name, arguments=arguments),
            ),
        )


class WorkflowSearch:
    name = "public-workflow-search"
    source_type: SourceType = "web"
    configured = True

    def __init__(self, fixture: WorkflowFixture) -> None:
        self.fixture = fixture
        self.calls = 0

    async def close(self) -> None:
        pass

    async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        if query not in self.fixture.sources or self.calls >= 6:
            raise ValueError("Unexpected public workflow search")
        self.calls += 1
        result = SearchResult(
            source_type="web",
            provider=self.name,
            title=query,
            url=f"https://example.org/autoscholar-public/{query}",
            content=self.fixture.sources[query].content,
            external_id=query,
        )
        return SearchResponse(
            provider=self.name, source_type="web", query=query, results=(result,)[:limit]
        )


class WorkflowFaultSandbox:
    """Fixed transport fault AFTER real execution; no fabricated successful execution."""

    def __init__(self, sandbox: SandboxExecutor, fault: str) -> None:
        if fault not in ("none", "invalid_metrics_once", "tampered_accuracy"):
            raise ValueError("Unknown workflow fault")
        self.sandbox = sandbox
        self.fault = fault
        self.mutations = 0

    async def health(self) -> SandboxHealth:
        return await self.sandbox.health()

    async def close(self) -> None:
        pass

    async def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        import base64
        import hashlib
        import json

        result = await self.sandbox.run(request)
        if (
            request.action != "run_python"
            or request.path != "train.py"
            or result.status != "succeeded"
            or result.exit_code != 0
            or self.fault == "none"
            or (self.fault == "invalid_metrics_once" and self.mutations)
        ):
            return result
        changed = []
        for artifact in result.artifacts:
            if artifact.path != "outputs/raw_metrics.json":
                changed.append(artifact)
                continue
            raw = base64.b64decode(artifact.data_base64, validate=True)
            if (
                len(raw) != artifact.size_bytes
                or hashlib.sha256(raw).hexdigest() != artifact.sha256
            ):
                raise ValueError("Cannot inject a fault into an invalid transport artifact")
            payload = decode_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected actual training metrics")
            if self.fault == "invalid_metrics_once":
                payload["runs"] = []
            else:
                runs = payload["runs"]
                if not isinstance(runs, list) or len(runs) != 2:
                    raise ValueError("Expected two real training runs")
                first, second = runs
                if not isinstance(first, dict) or not isinstance(second, dict):
                    raise ValueError("Expected measured model records")
                first["test_accuracy"] = 0.123456
                second["test_accuracy"] = 0.987654
            modified = json.dumps(payload, allow_nan=False).encode()
            changed.append(
                SandboxArtifact(
                    path=artifact.path,
                    size_bytes=len(modified),
                    sha256=hashlib.sha256(modified).hexdigest(),
                    data_base64=base64.b64encode(modified).decode(),
                )
            )
            self.mutations += 1
        if not self.mutations:
            raise ValueError("Real training artifact not available for requested fault")
        return result.model_copy(update={"artifacts": changed})
