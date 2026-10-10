"""Actual control paths with explicit fake training; real local Qdrant and separate Docker QA."""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from autoscholar.evaluation import __main__ as cli
from autoscholar.evaluation.ablation_report import comparison, write_comparison
from autoscholar.evaluation.component_fixture import load_fixture
from autoscholar.evaluation.datasets import load_suite
from autoscholar.evaluation.e2e_adapter import InjectedWorkflowAdapter, WorkflowAblationLabels
from autoscholar.evaluation.models import RunConfiguration
from autoscholar.evaluation.retrieval_ablation import (
    InjectedRetrievalAblationAdapter,
    LexicalReranker,
    RetrievalFixture,
    RetrievalObservation,
    RetrievalPairAudit,
    Vocabulary,
)
from autoscholar.evaluation.runner import run_evaluation
from autoscholar.evaluation.workflow_ablation import WORKFLOW_VARIANTS, WorkflowVariant
from autoscholar.evaluation.workflow_fixture import WorkflowFixture
from autoscholar.orchestration.durable_models import ProjectMemoryRow
from tests.test_evaluation_workflow import RESOURCES, ROOT, WorkflowReplaySandbox

PUBLIC = ROOT / "benchmarks/ablations"


def workflow(tmp_path: Path, variant: WorkflowVariant) -> InjectedWorkflowAdapter:
    fixture, digest = load_fixture(PUBLIC / "workflow_fixture_v1.json", WorkflowFixture)
    return InjectedWorkflowAdapter(
        fixture,
        fixture_sha256=digest,
        sandbox=WorkflowReplaySandbox(),
        resources=RESOURCES,
        workspace_root=tmp_path,
        ablation=True,
        variant=variant,
    )


@pytest.mark.parametrize("variant", WORKFLOW_VARIANTS)
@pytest.mark.parametrize("index", range(3))
async def test_actual_workflow_ablation_paths(
    tmp_path: Path, variant: WorkflowVariant, index: int
) -> None:
    suite, _ = load_suite(PUBLIC / "workflow_v1.json")
    case = suite.cases[index]
    adapter = workflow(tmp_path, variant)
    adapter.validate_case(case)
    observation = await adapter.execute(case.prompt, case.inputs, seed=42)
    score = adapter.score(observation, case.expected)
    assert all(score.checks.values()), score.checks
    path = observation.payload["execution_path"]
    assert isinstance(path, dict)
    assert path["planner_calls"] == int(variant != "no_planner")
    assert path["static_plan"] == (variant == "no_planner")
    assert path["reviewer_calls"] == (0 if variant == "no_reviewer" else path["saved_reviews"])
    assert path["memory_reads"] == (0 if variant == "no_memory" else (2 if index == 1 else 1))
    assert observation.usage.external_api_calls == observation.usage.total_tokens == 0
    assert observation.usage.budget_tokens == 3 * (observation.usage.model_calls or 0)
    assert score.task_success == (index == 0 or (index == 1 and variant != "no_replanning"))
    if index == 1 and variant == "no_replanning":
        assert observation.payload["error_code"] == "evaluation_replanning_disabled"
        assert observation.payload["training_runs"] == 1
        assert not observation.payload["report_published"]
        assert observation.payload["grading_sandbox_runs"] == 0
    if index == 1 and variant == "no_reviewer":
        assert path["saved_reviews"] == 2  # Real rules force recovery despite no LLM review.
        assert score.task_success is True
    if index == 2:
        assert score.task_success is False  # No ablation removes independent checkpoint grading.


@pytest.mark.parametrize("variant", WORKFLOW_VARIANTS)
async def test_ablation_restart_keeps_memory_and_usage(
    tmp_path: Path, variant: WorkflowVariant
) -> None:
    fixture, digest = load_fixture(
        ROOT / "benchmarks/end_to_end/workflow_fixture_v1.json", WorkflowFixture
    )
    suite, _ = load_suite(ROOT / "benchmarks/end_to_end/public_v1.json")
    case = suite.cases[1]
    adapter = InjectedWorkflowAdapter(
        fixture,
        fixture_sha256=digest,
        sandbox=WorkflowReplaySandbox(),
        resources=RESOURCES,
        workspace_root=tmp_path,
        ablation=True,
        variant=variant,
    )
    observation = await adapter.execute(case.prompt, case.inputs, seed=42)
    score = adapter.score(observation, {"baseline": case.expected, "overrides": {}})
    assert all(score.checks.values()), score.checks
    assert observation.payload["resume_verified"] is True
    assert observation.payload["step_runs"] == {"research": 1, "code": 1, "train": 1}
    from sqlalchemy import URL
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(next(tmp_path.glob("evale-*/workflow.sqlite"))))
    )
    try:
        async with engine.connect() as connection:
            versions = (await connection.execute(select(ProjectMemoryRow.version))).scalars().all()
            assert versions == [1]  # Same seeded memory exists even when reads are bypassed.
    finally:
        await engine.dispose()


async def test_workflow_paired_report_separates_expected_refusal(tmp_path: Path) -> None:
    suite, digest = load_suite(PUBLIC / "workflow_v1.json")
    variants: tuple[WorkflowVariant, ...] = ("baseline", "no_replanning")
    directory = await run_evaluation(
        suite,
        dataset_sha256=digest,
        adapters=[workflow(tmp_path / "work", variant) for variant in variants],
        configuration=RunConfiguration(profile="injected", case_timeout_seconds=60.0),
        output_root=tmp_path / "reports",
        repo_root=ROOT,
    )
    from autoscholar.evaluation.models import CaseResult

    records = [
        CaseResult.model_validate_json(line)
        for line in (directory / "cases.jsonl").read_text().splitlines()
    ]
    result = comparison(records, planned=3)
    contrast = result["contrasts"][0]
    assert contrast["matched"] == contrast["scored_pairs"] == 3
    assert contrast["metrics"]["task_completion"]["mean_delta"] == pytest.approx(-1 / 3)
    assert contrast["metrics"]["training_runs"]["mean_delta"] == pytest.approx(-1 / 3)
    broken = records[-1].model_copy(update={"input_sha256": "f" * 64})
    assert comparison([*records[:-1], broken], planned=3)["contrasts"][0]["matched"] == 2
    unknown = records[-1].model_copy(update={"score": None})
    assert comparison([*records[:-1], unknown], planned=3)["contrasts"][0]["scored_pairs"] == 2
    different_resources = records[-1].model_copy(
        update={
            "adapter": records[-1].adapter.model_copy(
                update={"resources": {"sandbox_image": "f" * 64}}
            )
        }
    )
    assert (
        comparison([*records[:-1], different_resources], planned=3)["contrasts"][0]["matched"] == 2
    )
    write_comparison(directory, planned=3)
    report = (directory / "evaluation_report.md").read_text(encoding="utf-8")
    assert "## Controlled ablations" in report and "Scored pairs / planned" in report
    assert "-0.333333" in report and (directory / "ablation_comparison.json").is_file()


def test_unknown_or_implicit_workflow_ablations_rejected(tmp_path: Path) -> None:
    fixture, digest = load_fixture(PUBLIC / "workflow_fixture_v1.json", WorkflowFixture)
    with pytest.raises(ValueError):
        InjectedWorkflowAdapter(
            fixture,
            fixture_sha256=digest,
            sandbox=WorkflowReplaySandbox(),
            resources=RESOURCES,
            workspace_root=tmp_path,
            variant="no_planner",
        )
    suite, _ = load_suite(PUBLIC / "workflow_v1.json")
    labels = suite.cases[0].expected
    with pytest.raises(ValidationError):
        WorkflowAblationLabels.model_validate(
            {**labels, "overrides": {"no_reranker": labels["baseline"]}}
        )


@pytest.mark.parametrize("index", range(5))
async def test_actual_qdrant_paired_candidate_pool(tmp_path: Path, index: int) -> None:
    fixture, digest = load_fixture(PUBLIC / "retrieval_fixture_v1.json", RetrievalFixture)
    suite, _ = load_suite(PUBLIC / "retrieval_v1.json")
    case = suite.cases[index]
    audit = RetrievalPairAudit()
    observations = []
    for variant in ("baseline", "no_reranker"):
        adapter = InjectedRetrievalAblationAdapter(
            fixture, fixture_sha256=digest, variant=variant, workspace_root=tmp_path, audit=audit
        )
        adapter.validate_case(case)
        observation = await adapter.execute(case.prompt, case.inputs, seed=42)
        score = adapter.score(observation, case.expected)
        assert all(score.checks.values()), score.checks
        assert score.task_success is None
        assert observation.usage.external_api_calls == observation.usage.model_calls == 0
        assert observation.usage.monetary_cost is None
        observations.append(RetrievalObservation.model_validate(observation.payload))
    assert observations[0].candidates == observations[1].candidates
    assert observations[0].reranker_calls == 1 and observations[1].reranker_calls == 0
    assert len(observations[0].candidates) == 8
    assert observations[1].paired_candidates is True


def test_no_reranker_missing_baseline_cannot_claim_pairing() -> None:
    audit = RetrievalPairAudit()
    assert audit.observe("no_reranker", "network", 42, ["a"]) is False
    audit.observe("baseline", "network", 42, ["a", "b"])
    assert audit.observe("no_reranker", "network", 42, ["b", "a"]) is False
    assert audit.observe("no_reranker", "network", 43, ["a", "b"]) is False


async def test_lexical_reranker_computes_text_not_ids() -> None:
    from dataclasses import replace

    from tests.test_evaluation_rag import chunk

    entries = [
        replace(chunk("a"), content="unrelated material"),
        replace(chunk("b"), content="checkpoint test accuracy"),
    ]
    ranked = await LexicalReranker().rerank("checkpoint accuracy", entries, limit=2)
    assert [item.id for item in ranked] == ["b", "a"]
    assert [item.id for item in await LexicalReranker().rerank("unrelated", entries, limit=2)] == [
        "a",
        "b",
    ]


def test_controlled_vocabulary_bounds() -> None:
    fixture, _ = load_fixture(PUBLIC / "retrieval_fixture_v1.json", RetrievalFixture)
    vocabulary = Vocabulary(fixture)
    assert vocabulary.counts("test test") == {vocabulary.words["test"]: 2.0}
    with pytest.raises(ValueError):
        vocabulary.counts("中文")  # This controlled fixture makes no multilingual claim.


@pytest.mark.parametrize(
    "arguments",
    [
        ["--category", "rag", "--ablations"],
        ["--category", "rag", "--ablations", "--profile", "injected", "--modes", "hybrid"],
        ["--category", "tool", "--ablations"],
        ["--category", "end_to_end", "--ablations", "--profile", "injected"],
    ],
)
def test_cli_ablation_requires_explicit_supported_injection(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["eval", "run", *arguments])
    assert cli.main() == 2
