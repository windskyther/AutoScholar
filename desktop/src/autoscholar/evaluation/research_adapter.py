"""Actual Research Agent execution with bounded scripted providers and an annotated corpus.

Citation syntax is NOT entailment. Semantic metrics below use explicit, closed-world
claim/source annotations, bound to exact source snapshots; they are not a general NLI judge.
"""

import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AfterValidator, Field, JsonValue, model_validator
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoscholar.agent.database_models import Base
from autoscholar.agent.repository import AgentTaskRepository
from autoscholar.agent.runner import AgentRunError, AgentRunner
from autoscholar.evaluation.component_fixture import QueryInputs, VersionedFixture, fixture_usage
from autoscholar.evaluation.datasets import payload_digest
from autoscholar.evaluation.models import (
    AdapterIdentity,
    BenchmarkCase,
    EvaluationModel,
    Identifier,
    Observation,
    ScoreCard,
)
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.research.models import SearchResponse, SearchResult, SourceType
from autoscholar.research.providers import SearchProviderError

TOPICS = ("mnist", "sandbox", "approval")


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Fixture text must not be blank")
    return value


Text = Annotated[str, Field(min_length=1, max_length=8192), AfterValidator(_nonblank)]
UnitScore = Annotated[float, Field(ge=0, le=1)]
ResearchError = Literal["research_evidence_unavailable", "invalid_research_citations"]


class PublicSource(EvaluationModel):
    topic: Literal["mnist", "sandbox", "approval"]
    content: Text


class ClaimReferences(EvaluationModel):
    claim: Text
    evidence_ids: list[Annotated[str, Field(pattern=r"^E[1-9][0-9]{0,2}$")]] = Field(
        min_length=1, max_length=12
    )


class ResearchScript(EvaluationModel):
    prompt: Text
    source_ids: list[Identifier] = Field(max_length=12)
    selection_ids: list[Identifier] = Field(max_length=12)
    answer: Text
    citations: list[ClaimReferences] = Field(max_length=12)
    provider_state: Literal["ready", "unavailable", "error"] = "ready"


class ResearchFixture(VersionedFixture):
    sources: dict[Identifier, PublicSource] = Field(min_length=1, max_length=100)
    scripts: dict[Identifier, ResearchScript] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def references(self) -> "ResearchFixture":
        for script in self.scripts.values():
            if not set(script.source_ids) <= self.sources.keys():
                raise ValueError("Unknown search fixture source")
            if not set(script.selection_ids) <= set(script.source_ids):
                raise ValueError("Unknown selected fixture source")
            if len(set(script.selection_ids)) != len(script.selection_ids):
                raise ValueError("Duplicate evidence selections")
        return self


class SourceAnnotation(EvaluationModel):
    id: Identifier
    content: Text
    relevance: UnitScore
    quality: UnitScore


class ClaimAnnotation(EvaluationModel):
    claim: Text
    source_ids: list[Identifier] = Field(min_length=1, max_length=12)


def _claim(text: str) -> str:
    return " ".join(text.split())


class ResearchLabels(EvaluationModel):
    status: Literal["succeeded", "failed"]
    error_code: ResearchError | None = None
    warning_code: Literal["web_search_not_configured", "fixture_search_error"] | None = None
    sources: list[SourceAnnotation] = Field(min_length=1, max_length=100)
    claims: list[ClaimAnnotation] = Field(min_length=1, max_length=12)
    minimum_relevance: UnitScore = 1.0
    minimum_quality: UnitScore = 0.5

    @model_validator(mode="after")
    def coherent(self) -> "ResearchLabels":
        ids = {source.id for source in self.sources}
        if len(ids) != len(self.sources):
            raise ValueError("Duplicate source annotations")
        if len({_claim(item.claim) for item in self.claims}) != len(self.claims):
            raise ValueError("Duplicate claim annotations")
        if any(not set(item.source_ids) <= ids for item in self.claims):
            raise ValueError("Claim annotation has an unknown source")
        if (self.status == "failed") != bool(self.error_code):
            raise ValueError("A failed Research case needs a specific expected error")
        return self


class ObservedEvidence(EvaluationModel):
    key: str = Field(pattern=r"^E[1-9][0-9]{0,2}$")
    source_id: str = Field(min_length=1, max_length=200)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResearchObservation(EvaluationModel):
    status: Literal["succeeded", "partial", "failed"]
    error_code: ResearchError | None = None
    answer: str = Field(max_length=100000)
    evidence: list[ObservedEvidence] = Field(max_length=12)
    citations: list[ClaimReferences] = Field(max_length=12)
    warnings: list[str] = Field(max_length=100)
    searches: int = Field(ge=0, le=6)


def score_research(observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
    labels = ResearchLabels.model_validate(expected)
    actual = ResearchObservation.model_validate(observation.payload)
    sources = {item.id: item for item in labels.sources}
    evidence = {item.key: item for item in actual.evidence}
    claims = {_claim(item.claim): set(item.source_ids) for item in labels.claims}
    bound = {
        key: item.source_id
        for key, item in evidence.items()
        if item.source_id in sources
        and item.content_sha256 == payload_digest(sources[item.source_id].content)
    }
    pairs = {
        (_claim(citation.claim), key)
        for citation in actual.citations
        for key in citation.evidence_ids
    }
    inline = set(re.findall(r"\[(E[0-9]+)\]", actual.answer))
    mapped = {key for _, key in pairs}
    syntax_valid = bool(pairs) and inline == mapped and mapped <= evidence.keys()
    answer = _claim(actual.answer)
    inline_pairs = {
        (claim, key)
        for claim in {text for text, _ in pairs}
        for match in re.finditer(re.escape(claim) + r"\s*((?:\[E[0-9]+\]\s*)+)", answer)
        for key in re.findall(r"\[(E[0-9]+)\]", match.group(1))
    }
    supported = {
        (claim, key)
        for claim, key in pairs
        if claim in claims and bound.get(key) in claims[claim] and (claim, key) in inline_pairs
    }
    supported_claims = {claim for claim, _ in supported}
    support_rate = len(supported_claims) / len(claims)
    # Duplicate references/evidence never add credit. Unannotated/tampered sources get zero.
    selected = {item.source_id for item in actual.evidence}
    intact = set(bound.values())
    relevance = (
        sum(sources[source].relevance for source in selected & intact) / len(selected)
        if selected
        else None
    )
    quality = (
        sum(sources[source].quality for source in selected & intact) / len(selected)
        if selected
        else None
    )
    correctness = len(supported) / len(pairs) if pairs else None
    checks = {
        "research_status": actual.status == labels.status,
        "error_classification": actual.error_code == labels.error_code,
        "expected_warning": labels.warning_code is None or labels.warning_code in actual.warnings,
    }
    if labels.status == "succeeded":
        checks.update(
            {
                "citation_validity": syntax_valid,
                "inline_claim_bindings": bool(pairs) and pairs <= inline_pairs,
                "citation_correctness": correctness == 1.0,
                "claim_support": support_rate == 1.0,
                "source_snapshots": len(bound) == len(actual.evidence),
                "evidence_relevance": relevance is not None
                and relevance >= labels.minimum_relevance,
                "source_quality": quality is not None and quality >= labels.minimum_quality,
            }
        )
    else:
        checks["no_failed_report_published"] = not actual.answer and not actual.citations
    return ScoreCard(
        checks=checks,
        metrics={
            "research_completion": float(actual.status == "succeeded"),
            "citation_validity": float(syntax_valid),
            "citation_correctness": correctness,
            "claim_support": support_rate,
            "evidence_relevance": relevance,
            "source_quality": quality,
            "selected_sources": float(len(selected)),
            "local_search_calls": float(actual.searches),
            "provider_unavailable": float("web_search_not_configured" in actual.warnings),
            "provider_error": float("fixture_search_error" in actual.warnings),
            "citation_protocol_error": float(actual.error_code == "invalid_research_citations"),
            "evidence_unavailable": float(actual.error_code == "research_evidence_unavailable"),
            "expected_rejection": float(labels.status == "failed"),
        },
    )


def _candidate_ids(fixture: ResearchFixture, script: ResearchScript) -> list[str]:
    # Match the real runner's per-query candidate cap BEFORE cross-query deduplication.
    return list(
        dict.fromkeys(
            source_id
            for topic in TOPICS
            for source_id in [
                identifier
                for identifier in script.source_ids
                if fixture.sources[identifier].topic == topic
            ][:3]
        )
    )


class _ScriptedProvider:
    def __init__(self, fixture: ResearchFixture, script: ResearchScript) -> None:
        self.fixture = fixture
        self.script = script
        self.calls = 0
        self.candidates = _candidate_ids(fixture, script)

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
        self.calls += 1
        if self.calls > 8 or not tools:
            raise ValueError("Scripted provider call bound exceeded")
        name = tools[0].name
        arguments: dict[str, object]
        if name == "submit_plan":
            arguments = {"mode": "research", "steps": ["Search public fixtures", "Cite evidence"]}
        elif name == "submit_research_queries":
            arguments = {
                "queries": [
                    {"topic": topic, "query": topic, "source_type": "web", "purpose": "Public test"}
                    for topic in TOPICS
                ]
            }
        elif name == "submit_evidence":
            arguments = {
                "items": [
                    {
                        "candidate_id": f"C{self.candidates.index(source_id) + 1}",
                        "claim": self.fixture.sources[source_id].content,
                        "relevance": 1.0,
                    }
                    for source_id in self.script.selection_ids
                ]
            }
        elif name == "submit_research_report":
            arguments = {
                "answer": self.script.answer,
                "citations": [item.model_dump() for item in self.script.citations],
            }
        else:
            raise ValueError("Unexpected scripted provider stage")
        return LLMResult(
            text="",
            model="scripted-public-provider",
            usage=TokenUsage(0, 0, 0),
            tool_calls=(ToolCall(id=f"fixture-{self.calls}", name=name, arguments=arguments),),
        )


class _PublicSearch:
    name = "public-search-fixture"
    source_type: SourceType = "web"

    def __init__(self, fixture: ResearchFixture, script: ResearchScript) -> None:
        self.fixture = fixture
        self.script = script
        self.calls = 0

    @property
    def configured(self) -> bool:
        return self.script.provider_state != "unavailable"

    async def close(self) -> None:
        pass

    async def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        self.calls += 1
        if self.script.provider_state == "error":
            raise SearchProviderError(code="fixture_search_error", message="Public injected error")
        results = tuple(
            SearchResult(
                source_type="web",
                provider=self.name,
                title=source_id,
                url=f"https://example.org/autoscholar-public/{source_id}",
                content=self.fixture.sources[source_id].content,
                external_id=source_id,
            )
            for source_id in self.script.source_ids
            if self.fixture.sources[source_id].topic == query
        )
        return SearchResponse(
            provider=self.name, source_type="web", query=query, results=results[:limit]
        )


class FixtureResearchAdapter:
    def __init__(self, fixture: ResearchFixture, *, fixture_sha256: str) -> None:
        self.fixture = fixture
        self.identity = AdapterIdentity(
            name="local-research-agent-fixture",
            category="research",
            variant="baseline",
            execution="fixture",
            model="scripted-public-provider",
            resources={"provider_fixture": fixture_sha256},
        )

    def validate_case(self, case: BenchmarkCase) -> None:
        query = QueryInputs.model_validate(case.inputs)
        labels = ResearchLabels.model_validate(case.expected)
        script = self.fixture.scripts.get(query.query_id)
        if script is None or script.prompt != case.prompt:
            raise ValueError("Research fixture does not match benchmark prompt")
        if not set(script.selection_ids) <= set(_candidate_ids(self.fixture, script)):
            raise ValueError("Selected source is outside the real candidate limit")
        for source in labels.sources:
            if source.id not in self.fixture.sources:
                raise ValueError("Unknown annotated source")
            if source.content != self.fixture.sources[source.id].content:
                raise ValueError("Gold source snapshot differs from public fixture")

    async def execute(self, prompt: str, inputs: dict[str, JsonValue], *, seed: int) -> Observation:
        query = QueryInputs.model_validate(inputs)
        script = self.fixture.scripts[query.query_id]
        if script.prompt != prompt:
            raise ValueError("Research query changed after validation")
        provider = _ScriptedProvider(self.fixture, script)
        search = _PublicSearch(self.fixture, script)
        # Only this in-memory DB is created. Never load Settings, migrate or connect to app DB.
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            repository = AgentTaskRepository(async_sessionmaker(engine, expire_on_commit=False))
            runner = AgentRunner(
                provider=provider, repository=repository, tools=[], research_services=[search]
            )
            task_id = str(uuid4())
            try:
                await runner.run(prompt, task_id=task_id, mode="research", research_sources=["web"])
            except AgentRunError as exc:
                if exc.code not in ("research_evidence_unavailable", "invalid_research_citations"):
                    raise
            task = await repository.get_task(task_id)
            if task is None:
                raise ValueError("Research task did not persist")
            actual = ResearchObservation.model_validate(
                {
                    "status": task.status,
                    "error_code": task.error_code,
                    "answer": task.answer or "",
                    "evidence": [
                        {
                            "key": item.citation_key,
                            "source_id": item.external_id or "unknown",
                            "content_sha256": payload_digest(item.excerpt),
                        }
                        for item in task.evidence
                    ],
                    "citations": [
                        {"claim": item.claim, "evidence_ids": list(item.evidence_ids)}
                        for item in task.citations
                    ],
                    "warnings": [item.code for item in task.warnings],
                    "searches": search.calls,
                }
            )
            return Observation(
                payload=actual.model_dump(mode="json"),
                usage=fixture_usage(model_calls=provider.calls),
            )
        finally:
            await engine.dispose()

    def score(self, observation: Observation, expected: dict[str, JsonValue]) -> ScoreCard:
        return score_research(observation, expected)
