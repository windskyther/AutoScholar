"""Real local flows and adversarial scoring tests; no provider requests are permitted."""

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation import research_adapter as research_module
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.models import BenchmarkCase, Observation, RunConfiguration
from autoscholar.evaluation.research_adapter import (
    FixtureResearchAdapter,
    ResearchFixture,
    ResearchLabels,
    score_research,
)
from autoscholar.evaluation.runner import run_evaluation
from autoscholar.evaluation.tool_adapter import FixtureToolAdapter, ToolFixture, score_tool
from autoscholar.llm.models import ConversationMessage, LLMResult, ToolChoice, ToolDefinition

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "benchmarks/research"
TOOL = ROOT / "benchmarks/tool"


def research_adapter() -> tuple[FixtureResearchAdapter, BenchmarkCase]:
    fixture, digest = load_fixture(RESEARCH / "provider_fixture_v1.json", ResearchFixture)
    suite, _ = load_suite(RESEARCH / "public_v1.json")
    return FixtureResearchAdapter(fixture, fixture_sha256=digest), suite.cases[0]


def tool_adapter() -> tuple[FixtureToolAdapter, BenchmarkCase]:
    fixture, digest = load_fixture(TOOL / "call_fixture_v1.json", ToolFixture)
    suite, _ = load_suite(TOOL / "public_v1.json")
    return FixtureToolAdapter(fixture, fixture_sha256=digest), suite.cases[0]


@pytest.fixture(autouse=True)
def prohibit_external_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoscholar.core.config import Settings

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "Offline component fixtures must not instantiate Settings/HTTP clients"
        )

    monkeypatch.setattr(Settings, "__init__", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setenv("LLM_API_KEY", "never-send-private-key")
    monkeypatch.setenv("TAVILY_API_KEY", "never-send-private-search-key")


async def test_all_ten_research_cases_run_real_agent_and_independent_local_sqlite() -> None:
    adapter, _ = research_adapter()
    suite, _ = load_suite(RESEARCH / "public_v1.json")
    for case in suite.cases:
        adapter.validate_case(case)
        observation = await adapter.execute(case.prompt, case.inputs, seed=42)
        score = adapter.score(observation, case.expected)
        assert all(score.checks.values()), (case.id, score)
        assert score.task_success is None
        assert observation.usage.external_api_calls == 0
        assert observation.usage.total_tokens == 0
        assert observation.usage.monetary_cost is None
        if case.expected["status"] == "succeeded":
            assert observation.usage.model_calls == 4
            assert score.metrics["selected_sources"] == 3.0
            assert score.metrics["claim_support"] == 1.0
        else:
            assert score.metrics["research_completion"] == 0.0
            assert score.metrics["claim_support"] == 0.0
    assert len(suite.cases) == 10


async def test_twenty_tool_cases_execute_or_reject_without_paid_calls() -> None:
    adapter, _ = tool_adapter()
    suite, _ = load_suite(TOOL / "public_v1.json")
    invocations = 0.0
    successes = 0.0
    for case in suite.cases:
        adapter.validate_case(case)
        observation = await adapter.execute(case.prompt, case.inputs, seed=42)
        score = adapter.score(observation, case.expected)
        assert all(score.checks.values()), (case.id, score)
        invocations += score.metrics["tool_invocations"] or 0
        successes += score.metrics["execution_success"] or 0
        assert observation.usage.model_calls == 0
        assert observation.usage.external_api_calls == 0
        if not case.expected["succeeded"]:
            assert score.metrics["result_accuracy"] is None
            assert score.metrics["execution_success"] == 0.0
    assert len(suite.cases) == 20
    assert (invocations, successes) == (16, 12)


@pytest.mark.parametrize(
    "mutation",
    [
        "unsupported",
        "wrong_source",
        "claim_not_in_answer",
        "inline_sources_swapped",
    ],
)
async def test_agent_success_does_not_mask_semantic_citation_failures(mutation: str) -> None:
    adapter, case = research_adapter()
    script = adapter.fixture.scripts["basic"]
    if mutation == "unsupported":
        script.citations[0].claim = "The public fixture proves every model is perfect."
        script.answer = script.answer.replace(
            "The public fixture uses 2048 training examples and 1024 test examples.",
            script.citations[0].claim,
        )
    elif mutation == "wrong_source":
        script.citations[0].evidence_ids = ["E2"]
        script.citations[1].evidence_ids = ["E1"]
    elif mutation == "inline_sources_swapped":
        script.answer = script.answer.replace("[E1]", "[TEMP]").replace("[E2]", "[E1]")
        script.answer = script.answer.replace("[TEMP]", "[E2]")
    else:
        script.answer = "A different sentence. [E1] Network summary. [E2] Approval summary. [E3]"
    adapter.validate_case(case)
    observation = await adapter.execute(case.prompt, case.inputs, seed=42)
    assert observation.payload["status"] == "succeeded"
    score = adapter.score(observation, case.expected)
    assert score.checks["citation_validity"]
    assert not score.checks["citation_correctness"]
    assert not score.checks["claim_support"]
    assert score.metrics["citation_correctness"] != 1.0


@pytest.mark.parametrize(
    "mutation",
    [
        "tampered_snapshot",
        "unknown_source",
        "missing_claim",
        "missing_inline",
    ],
)
async def test_research_independent_oracle_detects_observation_defects(mutation: str) -> None:
    adapter, case = research_adapter()
    actual = await adapter.execute(case.prompt, case.inputs, seed=42)
    payload = deepcopy(actual.payload)
    evidence = payload["evidence"]
    citations = payload["citations"]
    assert isinstance(evidence, list) and isinstance(evidence[0], dict)
    assert isinstance(citations, list)
    if mutation == "tampered_snapshot":
        evidence[0]["content_sha256"] = "0" * 64
    elif mutation == "unknown_source":
        evidence[0]["source_id"] = "unannotated-source"
    elif mutation == "missing_claim":
        payload["citations"] = citations[1:]
    else:
        payload["answer"] = str(payload["answer"]).replace("[E1]", "")
    score = score_research(Observation(payload=payload), case.expected)
    assert not all(score.checks.values())
    if mutation in ("tampered_snapshot", "unknown_source"):
        assert score.metrics["claim_support"] == pytest.approx(2 / 3)
        assert score.metrics["evidence_relevance"] == pytest.approx(2 / 3)


async def test_duplicate_references_do_not_inflate_metrics() -> None:
    adapter, case = research_adapter()
    original = await adapter.execute(case.prompt, case.inputs, seed=42)
    before = score_research(original, case.expected)
    payload = deepcopy(original.payload)
    citations = payload["citations"]
    assert isinstance(citations, list)
    payload["citations"] = citations + citations
    after = score_research(Observation(payload=payload), case.expected)
    assert before.metrics == after.metrics


async def test_low_relevance_and_low_quality_are_independent_from_valid_ids() -> None:
    adapter, case = research_adapter()
    actual = await adapter.execute(case.prompt, case.inputs, seed=42)
    gold = deepcopy(case.expected)
    sources = gold["sources"]
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    sources[0].update({"relevance": 0.0, "quality": 0.0})
    gold["minimum_quality"] = 0.9
    score = score_research(actual, gold)
    assert score.checks["citation_validity"] and score.checks["claim_support"]
    assert not score.checks["evidence_relevance"]
    assert not score.checks["source_quality"]


@pytest.mark.parametrize("mutation", ["wrong_tool", "wrong_arguments", "wrong_result"])
async def test_tool_selection_arguments_and_results_have_separate_checks(mutation: str) -> None:
    adapter, case = tool_adapter()
    actual = await adapter.execute(case.prompt, case.inputs, seed=42)
    payload = deepcopy(actual.payload)
    if mutation == "wrong_tool":
        payload["tool"] = "unrelated_tool"
    elif mutation == "wrong_arguments":
        payload["arguments_sha256"] = "0" * 64
    else:
        payload["result"] = 999.0
    score = score_tool(Observation(payload=payload), case.expected)
    assert not all(score.checks.values())
    assert score.metrics["result_accuracy"] == 0.0


async def test_extra_arguments_are_rejected_before_real_tool_execution() -> None:
    adapter, case = tool_adapter()
    adapter.fixture.calls["addition"].arguments["unexpected"] = True
    observation = await adapter.execute(case.prompt, case.inputs, seed=42)
    assert observation.payload["error_code"] == "invalid_arguments"
    assert observation.payload["invoked"] is False


@pytest.mark.parametrize(
    "expression",
    [
        "2^1000000000",
        "2**1000000000",
        "2**(2+2)",
        "2**True",
    ],
)
def test_unbounded_synchronous_tool_inputs_fail_preflight(expression: str) -> None:
    adapter, case = tool_adapter()
    adapter.fixture.calls["addition"].arguments = {"expression": expression}
    with pytest.raises(ValueError, match="bounded literal"):
        adapter.validate_case(case)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_component_fixture_versions_are_strict_integers(version: JsonValue) -> None:
    with pytest.raises(ValidationError):
        ToolFixture.model_validate({"schema_version": version, "suite_id": "suite", "calls": {}})


def test_research_gold_snapshot_and_prompt_mismatches_fail_preflight() -> None:
    adapter, case = research_adapter()
    adapter.fixture.sources["mnist"].content = "Changed snapshot."
    with pytest.raises(ValueError, match="snapshot"):
        adapter.validate_case(case)
    adapter, case = research_adapter()
    adapter.fixture.scripts["basic"].prompt = "Changed prompt."
    with pytest.raises(ValueError, match="prompt"):
        adapter.validate_case(case)


@pytest.mark.parametrize("bad_field", ["duplicate_claim", "blank_claim", "unknown_source"])
def test_invalid_closed_world_annotations_are_rejected(bad_field: str) -> None:
    _, case = research_adapter()
    gold = deepcopy(case.expected)
    claims = gold["claims"]
    assert isinstance(claims, list) and isinstance(claims[0], dict)
    if bad_field == "duplicate_claim":
        claims.append(deepcopy(claims[0]))
    elif bad_field == "blank_claim":
        claims[0]["claim"] = "   "
    else:
        claims[0]["source_ids"] = ["unknown-source"]
    with pytest.raises(ValidationError):
        ResearchLabels.model_validate(gold)


async def test_two_research_tasks_have_no_shared_persisted_evidence() -> None:
    adapter, case = research_adapter()
    first, second = await asyncio.gather(
        adapter.execute(case.prompt, case.inputs, seed=42),
        adapter.execute(case.prompt, case.inputs, seed=43),
    )
    assert first.payload == second.payload
    evidence = first.payload["evidence"]
    assert isinstance(evidence, list) and len(evidence) == 3


async def test_cancelled_research_releases_its_isolated_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    disposed: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def pause(
        self: research_module._ScriptedProvider,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self)
        await original_dispose(self, close=close)

    monkeypatch.setattr(research_module._ScriptedProvider, "generate", pause)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    adapter, case = research_adapter()
    task = asyncio.create_task(adapter.execute(case.prompt, case.inputs, seed=42))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(disposed) == 1


@pytest.mark.parametrize("category", ["research", "tool"])
def test_component_cli_repeats_and_exports_only_scores(
    category: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = RESEARCH if category == "research" else TOOL
    fixture = "provider_fixture_v1.json" if category == "research" else "call_fixture_v1.json"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation",
            "run",
            "--category",
            category,
            "--dataset",
            str(directory / "public_v1.json"),
            "--fixture",
            str(directory / fixture),
            "--repeats",
            "2",
        ],
    )
    assert cli.main() == 0
    run = next((tmp_path / "data/evaluation").iterdir())
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    group = summary["groups"][0]
    assert group["status_counts"] == {"passed": 20 if category == "research" else 40}
    assert group["task_success_rate"] is None
    records = (run / "cases.jsonl").read_text(encoding="utf-8")
    assert "never-send-private" not in records and '"answer"' not in records
    assert all(
        json.loads(line)["usage"]["external_api_calls"] == 0 for line in records.splitlines()
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--ks", "5"],
        ["--modes", "dense"],
        ["--modes", "baseline", "baseline"],
        ["--category", "rag"],
    ],
)
def test_cli_rejects_incompatible_component_options_before_starting(
    extra: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation",
            "run",
            "--dataset",
            str(TOOL / "public_v1.json"),
            "--fixture",
            str(TOOL / "call_fixture_v1.json"),
            *extra,
        ],
    )
    assert cli.main() == 2
    assert not (tmp_path / "data/evaluation").exists()


async def test_semantic_failure_is_persisted_as_failed_not_runtime_error(tmp_path: Path) -> None:
    adapter, case = research_adapter()
    adapter.fixture.scripts["basic"].citations[0].evidence_ids = ["E2"]
    adapter.fixture.scripts["basic"].citations[1].evidence_ids = ["E1"]
    suite, digest = load_suite(RESEARCH / "public_v1.json")
    suite.cases = [case]
    run = await run_evaluation(
        suite,
        dataset_sha256=digest,
        adapters=[adapter],
        configuration=RunConfiguration(),
        output_root=tmp_path,
        repo_root=ROOT,
    )
    record = json.loads((run / "cases.jsonl").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["reason"] == "checks_failed"
    assert record["score"]["checks"]["citation_correctness"] is False
