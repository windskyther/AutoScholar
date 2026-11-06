"""Research-only durable regressions use scripted models, source fixtures and SQLite."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import update

from autoscholar.agent.database_models import EvidenceRow
from autoscholar.agent.runner import AgentRunner
from autoscholar.api.routes.agent import AgentRunRequest
from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.core.errors import AppError
from autoscholar.llm import LLMResult
from autoscholar.llm.errors import LLMUnavailableError
from autoscholar.orchestration.checkpoints import Snapshot
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.models import ReviewIssue, ReviewResult, TaskPlan
from autoscholar.orchestration.review_facts import requirement_issue_error, step_facts
from autoscholar.rag.database_models import DocumentChunkRow, DocumentRow, ProjectRow
from autoscholar.research import ResearchSearchService, TavilySearchProvider
from autoscholar.research.context import research_context
from tests.test_agent_runner import ScriptedProvider
from tests.test_autonomous import call, workflow
from tests.test_durable_workflow import drain
from tests.test_experiment_service import FakeExperimentSandbox
from tests.test_research_agent import search_services, successful_responses
from tests.test_workflow_foundation import plan

OBJECTIVE = "2026年10月CV领域的最新研究成果"
RESEARCH_PLAN: dict[str, Any] = {
    "goal": OBJECTIVE,
    "steps": [
        {
            "id": "survey",
            "type": "research",
            "description": OBJECTIVE,
            "dependencies": [],
            "expected_output": "A source-grounded research report",
        }
    ],
}


def research_script() -> list[LLMResult]:
    research = successful_responses()
    # A supported paraphrase deliberately differs from the original source wording.
    research[2].tool_calls[0].arguments["items"][0]["claim"] = (
        "Low-rank updates adapt model weights"
    )
    return [
        call("submit_task_plan", RESEARCH_PLAN),
        *research,
        call("submit_review", ReviewResult(status="PASS").model_dump()),
    ]


async def research_service(tmp_path: Path, provider: ScriptedProvider) -> tuple[Any, Any]:
    service, engine = await workflow(tmp_path, provider, FakeExperimentSandbox())
    service.runner = AgentRunner(
        provider=service.provider,
        repository=service.tasks,
        tools=[],
        research_services=search_services(),
    )
    async with service.tasks.session_factory() as session:
        session.add(ProjectRow(id="research-project", name="Public research fixture"))
        await session.commit()
    return service, engine


async def test_research_survives_restart_without_code_or_training(tmp_path: Path) -> None:
    provider = ScriptedProvider(research_script())
    service, engine = await research_service(tmp_path, provider)
    durable = DurableService(service)
    try:
        task_id, _ = await durable.submit(
            {
                "objective": OBJECTIVE,
                "workflow_kind": "research",
                "project_id": "research-project",
            },
            "research-only",
        )
        snapshot, _ = await durable.repository.snapshot(task_id)
        assert snapshot.document_ids == []  # all-ready selection on an empty library
        assert snapshot.workflow_kind == "research"
        assert await durable.tick(task_id)
        assert await drain(DurableService(service), task_id) == "succeeded"
        task = await service.tasks.get_task(task_id)
        assert task and OBJECTIVE in (task.answer or "")
        assert "fixed MNIST subset" not in (task.answer or "")
        assert "LoRA" in (task.answer or "")  # scripted fixture report was retained
        assert task.metrics.get("training_runs", 0) == 0
        assert task.metrics.get("sandbox_runs", 0) == 0
        steps = await service.workflows.history(task_id, "steps")
        assert [(row["step_id"], row["status"]) for row in steps] == [("survey", "succeeded")]
        planner = json.loads(provider.calls[0]["messages"][1].content)
        assert planner["date_context"]["requested_start"] == "2026-10-01"
        assert "experiment_contract" not in planner
        review = json.loads(provider.calls[-1]["messages"][1].content)
        facts = review["verified_checks"]["survey"]
        assert facts["checks"]["verbatim_citation"]
        assert all(row["excerpt_in_source"] for row in facts["citations"])
        child = await service.tasks.get_task(steps[0]["child_task_id"])
        assert child and not any(t.tool_name == "knowledge_search" for t in child.tool_calls)
        assert child.evidence[0].claim not in child.evidence[0].excerpt
        assert len(provider.calls) == 6
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bad", ["experiment", "changed_goal", "disabled_documents"])
async def test_research_plan_cannot_replace_goal_or_expand_execution(
    tmp_path: Path,
    bad: str,
) -> None:
    proposed = json.loads(json.dumps(RESEARCH_PLAN))
    if bad == "experiment":
        proposed = plan().model_dump()
        proposed["goal"] = OBJECTIVE
    elif bad == "changed_goal":
        proposed["goal"] = "Compare MNIST models"
    else:
        proposed["steps"][0]["type"] = "knowledge"
    provider = ScriptedProvider([call("submit_task_plan", proposed) for _ in range(2)])
    service, engine = await research_service(tmp_path, provider)
    try:
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            {
                "objective": OBJECTIVE,
                "workflow_kind": "research",
                "project_id": "research-project",
                "document_ids": [],
            },
            "invalid-plan",
        )
        assert await durable.tick(task_id)
        _, job = await durable.repository.snapshot(task_id)
        assert job.status == "failed" and not job.pending_calls
        assert not await service.workflows.history(task_id, "steps")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("tampered", ["excerpt", "url"])
async def test_tampered_excerpt_cannot_pass_even_if_model_approves(
    tmp_path: Path, tampered: str
) -> None:
    provider = ScriptedProvider(research_script())
    service, engine = await research_service(tmp_path, provider)
    try:
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            {
                "objective": OBJECTIVE,
                "workflow_kind": "research",
                "project_id": "research-project",
                "document_ids": [],
            },
            "tampered-quote",
        )
        assert await durable.tick(task_id) and await durable.tick(task_id)
        steps = await service.workflows.history(task_id, "steps")
        async with service.tasks.session_factory() as session:
            await session.execute(
                update(EvidenceRow)
                .where(
                    EvidenceRow.task_id == steps[0]["child_task_id"],
                    EvidenceRow.citation_key == "E1",
                )
                .values(
                    **{
                        tampered: "A fabricated quotation absent from the search receipt"
                        if tampered == "excerpt"
                        else "https://unrelated.example.org/forged-source"
                    }
                )
            )
            await session.commit()
        assert await durable.tick(task_id)
        snapshot, _ = await durable.repository.snapshot(task_id)
        assert snapshot.stage == "replanner" and snapshot.review
        assert snapshot.review.status == "REPLAN"
        assert any(i.code == "source_quote_unverified" for i in snapshot.review.issues)
    finally:
        await engine.dispose()


async def test_timeout_keeps_unknown_call_and_exposes_cause_without_replay(tmp_path: Path) -> None:
    class TimeoutProvider(ScriptedProvider):
        async def generate(self, *args: Any, **kwargs: Any) -> LLMResult:
            if not self.responses:
                raise LLMUnavailableError(code="llm_timeout", message="Public simulated timeout")
            return await super().generate(*args, **kwargs)

    provider = TimeoutProvider(research_script()[:4])  # report generation times out
    service, engine = await research_service(tmp_path, provider)
    try:
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            {
                "objective": OBJECTIVE,
                "workflow_kind": "research",
                "project_id": "research-project",
                "document_ids": [],
            },
            "timeout",
        )
        assert await durable.tick(task_id) and await durable.tick(task_id)
        _, job = await durable.repository.snapshot(task_id)
        assert job.status == "recovery_required" and len(job.pending_calls) == 1
        assert job.error_code == "external_result_uncertain"
        overview = await WorkbenchRepository(service.tasks.session_factory).overview(
            task_id, limit=20, offset=0
        )
        assert overview.execution and overview.execution.cause_error_code == "llm_timeout"
        usage = dict(job.usage)
        assert not await durable.tick(task_id)
        _, after = await durable.repository.snapshot(task_id)
        assert after.usage == usage and after.pending_calls == job.pending_calls
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure", ["connect", "timeout", "http429", "http401"])
async def test_uncertain_search_stops_at_first_request_with_original_cause(
    tmp_path: Path, failure: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "connect":
            raise httpx.ConnectError("secret-key and private request body", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-key and private request body", request=request)
        return httpx.Response(int(failure[4:]), json={"error": "secret-key"})

    script = research_script()[:3]
    for query in script[2].tool_calls[0].arguments["queries"]:
        query["source_type"] = "web"
    provider = ScriptedProvider(script)
    # File-backed SQLite exercises checkpoint/restart behavior, never the user's DB.
    service, engine = await workflow(
        tmp_path / "workspace",
        provider,
        FakeExperimentSandbox(),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'search.sqlite'}",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service.runner = AgentRunner(
            provider=service.provider,
            repository=service.tasks,
            tools=[],
            research_services=[
                ResearchSearchService(
                    provider=TavilySearchProvider(
                        api_key="secret-key",
                        client=client,
                        retry_delays=(),
                    )
                )
            ],
        )
        try:
            durable = DurableService(service)
            task_id, _ = await durable.submit(
                {
                    "objective": OBJECTIVE,
                    "workflow_kind": "research",
                    "research_sources": ["web"],
                },
                "search-failure",
            )
            assert await durable.tick(task_id) and await durable.tick(task_id)
            snapshot, job = await durable.repository.snapshot(task_id)
            expected = {
                "connect": "tavily_unavailable",
                "timeout": "tavily_unavailable",
                "http429": "tavily_upstream_error",
                "http401": "tavily_authentication_failed",
            }[failure]
            assert job.status == "recovery_required" and snapshot.stage == "executor"
            assert job.error_code == "external_result_uncertain"
            assert len(job.pending_calls) == 1
            assert next(iter(job.pending_calls.values())) == "search:tavily"
            assert len(requests) == 1 and len(provider.calls) == 3
            assert job.usage["search_queries"] == job.usage["tool_calls"] == 1
            assert job.usage["model_calls"] == 3
            assert job.usage["total_tokens"] == 9
            assert not await service.workflows.history(task_id, "reviews")
            steps = await service.workflows.history(task_id, "steps")
            assert len(steps) == 1 and steps[0]["result"]["error_code"] == expected
            child = await service.tasks.get_task(steps[0]["child_task_id"])
            assert child and len(child.tool_calls) == 1
            assert child.tool_calls[0].error_code == expected
            diagnostic = json.dumps(steps[0]["result"])
            assert "secret-key" not in diagnostic and "private request body" not in diagnostic
            if failure in {"connect", "timeout"}:
                assert ("ConnectError" if failure == "connect" else "ReadTimeout") in diagnostic
            overview = await WorkbenchRepository(service.tasks.session_factory).overview(
                task_id, limit=20, offset=0
            )
            assert overview.execution and overview.execution.cause_error_code == expected
            assert not await DurableService(service).tick(task_id)
            _, after = await durable.repository.snapshot(task_id)
            assert after.usage == job.usage and after.pending_calls == job.pending_calls
            assert len(requests) == 1 and len(provider.calls) == 3
        finally:
            await engine.dispose()


async def test_valid_quote_still_allows_reviewer_to_reject_unsupported_claim(
    tmp_path: Path,
) -> None:
    responses = research_script()
    responses[-1] = call(
        "submit_review",
        {
            "status": "REPLAN",
            "issues": [
                {
                    "code": "unsupported_claim",
                    "step_id": "survey",
                    "message": "The source does not establish the requested monthly finding",
                    "requirement_id": "research.citations",
                }
            ],
            "suggested_steps": ["survey"],
        },
    )
    provider = ScriptedProvider(responses)
    service, engine = await research_service(tmp_path, provider)
    try:
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            {
                "objective": OBJECTIVE,
                "workflow_kind": "research",
                "project_id": "research-project",
                "document_ids": [],
            },
            "unsupported-claim",
        )
        for _ in range(3):
            assert await durable.tick(task_id)
        snapshot, _ = await durable.repository.snapshot(task_id)
        assert snapshot.review and snapshot.review.status == "REPLAN"
        assert snapshot.review.issues[0].code == "unsupported_claim"
        assert len(provider.calls) == 6  # no correction rejects legitimate semantic review
    finally:
        await engine.dispose()


async def test_deleted_project_cannot_enqueue_a_new_research_task(tmp_path: Path) -> None:
    provider = ScriptedProvider([])
    service, engine = await research_service(tmp_path, provider)
    try:
        from datetime import UTC, datetime

        async with service.tasks.session_factory() as session:
            project = await session.get(ProjectRow, "research-project")
            assert project
            project.deleted_at = datetime.now(UTC)
            await session.commit()
        with pytest.raises(AppError, match="Project not found"):
            await DurableService(service).submit(
                {
                    "objective": OBJECTIVE,
                    "workflow_kind": "research",
                    "project_id": "research-project",
                },
                "deleted-project",
            )
        assert not provider.calls
    finally:
        await engine.dispose()


def test_verified_quote_does_not_automatically_prove_claim_support() -> None:
    issue = ReviewIssue(
        code="unsupported_claim",
        step_id="survey",
        message="The excerpt does not support the claim",
        requirement_id="research.citations",
    )
    assert (
        requirement_issue_error(
            issue,
            {
                "step_type": "research",
                "checks": {"verbatim_citation": True},
            },
            OBJECTIVE,
        )
        is None
    )


async def test_research_report_renumbers_child_citations_without_collisions(tmp_path: Path) -> None:
    service, engine = await research_service(tmp_path, ScriptedProvider([]))
    try:
        step = RESEARCH_PLAN["steps"][0]
        snapshot = Snapshot(
            task_id="report",
            objective=OBJECTIVE,
            workflow_kind="research",
            plan=TaskPlan.model_validate(
                {
                    "goal": OBJECTIVE,
                    "steps": [
                        {**step, "id": "first"},
                        {**step, "id": "second"},
                    ],
                }
            ),
            review=ReviewResult(status="PASS"),
            results={
                "first": {
                    "answer": "First [E1] and second [E2].",
                    "evidence": [
                        {"reference": "child-a:E1", "title": "One", "url": "https://example.org/1"},
                        {"reference": "child-a:E2", "title": "Two", "url": "https://example.org/2"},
                    ],
                },
                "second": {
                    "answer": "Third [E1] and fourth [E2].",
                    "evidence": [
                        {
                            "reference": "child-b:E1",
                            "title": "Three",
                            "url": "https://example.org/3",
                        },
                        {
                            "reference": "child-b:E2",
                            "title": "Four",
                            "url": "https://example.org/4",
                        },
                    ],
                },
            },
        )
        run = snapshot.restore({}, 0)
        await service._writer({"run": run})
        assert "First [E1] and second [E2]." in (run.answer or "")
        assert "Third [E3] and fourth [E4]." in (run.answer or "")
        assert "[E4] Four" in (run.answer or "")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("foreign", [False, True])
async def test_document_quote_verification_respects_chunk_project(
    tmp_path: Path, foreign: bool
) -> None:
    service, engine = await research_service(tmp_path, ScriptedProvider([]))
    try:
        source_project = "other-project" if foreign else "research-project"
        async with service.tasks.session_factory() as session:
            if foreign:
                session.add(ProjectRow(id=source_project, name="Other public fixture"))
                await session.flush()
            session.add(
                DocumentRow(
                    id="public-doc",
                    project_id=source_project,
                    original_filename="public.pdf",
                    title="Public fixture",
                    content_type="application/pdf",
                    storage_key="public-fixture",
                    sha256="a" * 64,
                    size_bytes=100,
                    status="ready",
                    page_count=1,
                )
            )
            await session.flush()
            session.add(
                DocumentChunkRow(
                    id="public-chunk",
                    project_id=source_project,
                    document_id="public-doc",
                    ordinal=0,
                    page=1,
                    content="The method uses low-rank updates.",
                    token_count=7,
                )
            )
            await session.commit()
        await service.tasks.create_task(
            task_id="knowledge-child",
            objective="Explain the document",
            mode="knowledge",
            project_id="research-project",
        )
        await service.tasks.add_evidence(
            task_id="knowledge-child",
            citation_key="E1",
            source_type="document",
            provider="qdrant",
            title="Public fixture",
            url="/projects/research-project/documents/public-doc/content#page=1",
            authors=(),
            year=None,
            external_id="public-chunk",
            query="Explain",
            topic="fixture",
            claim="A concise supported paraphrase",
            excerpt="The method uses low-rank updates.",
            relevance=1.0,
            document_id="public-doc",
            chunk_id="public-chunk",
            page=1,
        )
        facts = await step_facts(
            step_type="knowledge",
            result={"child_task_id": "knowledge-child"},
            tasks=service.tasks,
            workspace=service.workspace,
            artifacts=service.artifacts,
            specification=Snapshot(task_id="parent", objective="Explain").specification,
        )
        assert facts["checks"]["verbatim_citation"] is (not foreign)
    finally:
        await engine.dispose()


def test_month_context_and_legacy_checkpoint_compatibility() -> None:
    context = research_context(OBJECTIVE, date(2026, 11, 5))
    assert context["as_of_date"] == "2026-11-05"
    assert context["requested_start"] == "2026-10-01"
    assert context["requested_end"] == "2026-10-31"
    assert research_context("2024年2月论文", date(2024, 3, 1))["requested_end"] == "2024-02-29"
    legacy = Snapshot.model_validate({"task_id": "legacy", "objective": "Compare MNIST models"})
    assert legacy.restore({}, 0).workflow_kind == "experiment"
    with pytest.raises(ValidationError):
        AgentRunRequest(objective=OBJECTIVE, mode="research", workflow_kind="research")
    with pytest.raises(ValidationError):
        AgentRunRequest.model_validate(
            {
                "objective": OBJECTIVE,
                "mode": "autonomous",
                "workflow_kind": "research",
                "experiment_specification": {},
            }
        )
