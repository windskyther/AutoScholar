"""Local evidence lifecycle and public workbench contracts, without API calls."""

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select

from autoscholar.agent.runner import AgentProtocolError
from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.evidence.backfill import backfill
from autoscholar.evidence.database import EvidencePublicationRow, EvidenceReuseRow, ProjectClaimRow
from autoscholar.evidence.library import EvidenceRevoked
from autoscholar.evidence.models import current_evidence_guard
from autoscholar.llm.models import LLMResult
from autoscholar.orchestration.checkpoints import Snapshot
from autoscholar.orchestration.durable import DurableService
from autoscholar.orchestration.durable_models import WorkflowJobRow
from autoscholar.rag.database_models import DocumentRow, ProjectRow
from autoscholar.rag.embeddings import FastEmbedProvider
from autoscholar.rag.repository import KnowledgeRepository
from autoscholar.research.quality_models import Citation, Claim, Material
from tests.governance import offline_app
from tests.test_autonomous import call
from tests.test_health import FakeDependency
from tests.test_project_evidence_reuse import ReusingProvider, published


@pytest.mark.parametrize(
    "case", ["latest", "stale", "missing_type", "unknown", "duplicate", "partial", "valid"]
)
async def test_zero_queries_requires_fresh_coverage_and_valid_selection(
    tmp_path: Path, case: str
) -> None:
    service, engine, library, _ = await published(tmp_path)
    try:
        bank = await library.select(
            "research-project", "LoRA adaptation", document_ids=[], source_types=["web", "paper"]
        )
        args = dict(
            queries=[],
            reuse=[dict(claim_id=c.id, topic="LoRA", reason="Relevant") for c in bank.claims],
        )
        objective = "LoRA adaptation"
        if case == "latest":
            objective = "latest LoRA adaptation"
        if case == "stale":
            for source in bank.sources:
                source.fresh = False
        if case == "missing_type":
            for source in bank.sources:
                source.material.kind = "web"
        if case == "unknown":
            args["reuse"][0]["claim_id"] = "unknown"
        if case == "duplicate":
            args["reuse"].append(args["reuse"][0])
        if case == "partial":
            for source in bank.sources:
                if source.material.kind == "web":
                    source.fresh = False
            args["queries"] = [
                dict(
                    topic="LoRA",
                    query="new LoRA web evidence",
                    source_type="web",
                    purpose="Recheck stale source",
                )
            ]
        if case in {"valid", "partial"}:
            queries = service.runner._parse_queries(
                call("submit_research_queries", args),
                ["web", "paper"],
                bank=bank,
                objective=objective,
            )
            assert len(queries) == (1 if case == "partial" else 0)
        else:
            with pytest.raises(AgentProtocolError):
                service.runner._parse_queries(
                    call("submit_research_queries", args),
                    ["web", "paper"],
                    bank=bank,
                    objective=objective,
                )
    finally:
        await engine.dispose()


async def test_revocation_during_model_wait_accounts_usage_before_stopping(tmp_path: Path) -> None:
    service, engine, library, _ = await published(tmp_path)
    try:
        selected = await library.select(
            "research-project", "LoRA adaptation", document_ids=[], source_types=["web", "paper"]
        )
        budget = Budget(BudgetLimits())
        token = current_budget.set(budget)
        guard = current_evidence_guard.set(lambda: library.validate(selected, []))
        original = service.provider.provider.generate

        async def withdrawing(*args: object, **kwargs: object) -> LLMResult:
            await library.set_enabled("research-project", selected.claims[0].id, False)
            return call("fixture", {})

        service.provider.provider.generate = withdrawing
        try:
            with pytest.raises(EvidenceRevoked):
                await service.provider.generate([])
            assert budget.used["model_calls"] == 1 and budget.used["total_tokens"] > 0
            with pytest.raises(EvidenceRevoked):
                await service.provider.generate([])
            assert budget.used["model_calls"] == 1
        finally:
            service.provider.provider.generate = original
            current_evidence_guard.reset(guard)
            current_budget.reset(token)
        await library.set_enabled("research-project", selected.claims[0].id, True)
        await library.validate(selected, [])
        tampered = selected.model_copy(deep=True)
        tampered.sources[0].material.text = "forged"
        with pytest.raises(EvidenceRevoked):
            await library.validate(tampered, [])
    finally:
        await engine.dispose()


async def test_quick_reuse_receipts_track_sources_but_do_not_publish_unaudited_claims(
    tmp_path: Path,
) -> None:
    service, engine, library, _ = await published(tmp_path)
    try:
        objective = "Compare LoRA adaptation methods"
        service.provider.provider = ReusingProvider(objective)
        task_id, _ = await DurableService(service).submit(
            dict(
                objective=objective,
                project_id="research-project",
                workflow_kind="comparison",
                research_depth="quick",
                reuse_project_evidence=True,
            ),
            "quick-reuse",
        )
        from tests.test_research_quality import finish

        _, job = await finish(service, task_id)
        assert job.status == "succeeded", job.error_code
        async with library.sessions() as session:
            receipts = list(
                await session.scalars(
                    select(EvidenceReuseRow).where(EvidenceReuseRow.task_id == task_id)
                )
            )
            assert receipts and all(row.status == "adopted" for row in receipts)
            assert not await session.get(EvidencePublicationRow, task_id)
    finally:
        await engine.dispose()


async def test_history_import_is_explicit_pending_idempotent_and_preserves_reports(
    tmp_path: Path,
) -> None:
    service, engine, library, original_id = await published(tmp_path)
    try:
        before = await service.tasks.get_task(original_id)
        async with library.sessions() as session:
            await session.execute(delete(ProjectClaimRow))
            await session.execute(delete(EvidencePublicationRow))
            await session.commit()
        result = await backfill(library, "research-project")
        assert result["queued_tasks"] == 1
        assert (await backfill(library, "research-project"))["skipped_tasks"] == 1
        assert await library.publish_one()
        async with library.sessions() as session:
            await session.execute(delete(ProjectClaimRow))
            publication = await session.get(EvidencePublicationRow, original_id)
            assert publication
            publication.status, publication.attempts = "failed", 5
            await session.commit()
        assert (await backfill(library, "research-project"))["queued_tasks"] == 1
        assert await library.publish_one()
        await service.tasks.create_task(
            task_id="legacy",
            objective="LoRA legacy",
            project_id="research-project",
            mode="research",
        )
        await service.tasks.add_evidence(
            task_id="legacy",
            citation_key="E1",
            source_type="web",
            provider="fixture",
            title="Legacy source",
            url="https://example.org/legacy",
            authors=(),
            year=None,
            external_id=None,
            query="LoRA",
            topic="LoRA",
            claim="LoRA legacy claim",
            excerpt="Original legacy quote.",
            relevance=0.9,
        )
        await service.tasks.update_task(
            "legacy", status="succeeded", plan=[], answer="Original legacy answer", metrics={}
        )
        provider_calls = len(service.provider.provider.calls)
        first = await backfill(library, "research-project", limit=1)
        assert first["next_cursor"]
        second = await backfill(library, "research-project", after=first["next_cursor"], limit=1)
        assert first["pending_claims"] + second["pending_claims"] == 1
        assert not second["next_cursor"]
        assert (await backfill(library, "research-project"))["pending_claims"] == 0
        assert len(service.provider.provider.calls) == provider_calls
        async with library.sessions() as session:
            row = await session.scalar(
                select(ProjectClaimRow).where(ProjectClaimRow.task_id == "legacy")
            )
            assert row and row.review_status == "pending"
            assert not await library.valid_claim(session, row, project_id="research-project")
        after = await service.tasks.get_task(original_id)
        assert before and after and before.answer == after.answer and before.status == after.status
    finally:
        await engine.dispose()


async def test_pdf_scope_hash_and_deletion_withdraw_original_evidence(tmp_path: Path) -> None:
    _service, engine, library, task_id = await published(tmp_path)
    try:
        material = Material(
            id="E1",
            identity="document:pdf:page1",
            kind="document",
            title="Local LoRA PDF",
            url="project-document:pdf",
            text="LoRA original PDF passage",
            availability="document_excerpt",
            document_id="pdf",
            page=1,
        )
        async with library.sessions() as session:
            session.add(
                DocumentRow(
                    id="pdf",
                    project_id="research-project",
                    title="LoRA PDF",
                    original_filename="paper.pdf",
                    storage_key="pdf",
                    content_type="application/pdf",
                    sha256="a" * 64,
                    size_bytes=10,
                    status="ready",
                )
            )
            await session.flush()
            task = await library.visible_task(session, task_id, "research-project")
            assert task
            await library._claim(
                session,
                task,
                99,
                Claim(
                    text="LoRA PDF finding",
                    citations=[Citation(source_id="E1", quote=material.text)],
                ),
                {"E1": material},
                audit_hash="a" * 64,
            )
            await session.commit()
        selected = await library.select(
            "research-project",
            "LoRA PDF finding",
            document_ids=["pdf"],
            source_types=[],
            document_only=True,
        )
        assert len(selected.claims) == 1
        await library.validate(selected, ["pdf"])
        with pytest.raises(EvidenceRevoked):
            await library.validate(selected, [])
        async with library.sessions() as session:
            doc = await session.get(DocumentRow, "pdf")
            assert doc
            doc.sha256 = "b" * 64
            await session.commit()
        with pytest.raises(EvidenceRevoked):
            await library.validate(selected, ["pdf"])
        async with library.sessions() as session:
            doc = await session.get(DocumentRow, "pdf")
            assert doc
            doc.sha256 = "a" * 64
            doc.status = "deleting"
            await session.commit()
        with pytest.raises(EvidenceRevoked):
            await library.validate(selected, ["pdf"])
    finally:
        await engine.dispose()


async def test_evidence_api_is_scoped_readonly_bounded_and_persists_policy(tmp_path: Path) -> None:
    service, engine, library, task_id = await published(tmp_path)
    try:
        async with library.sessions() as session:
            session.add(ProjectRow(id="other-project", name="Other"))
            await session.commit()
        provider = service.provider.provider
        before = len(provider.calls)
        app = offline_app(
            Settings(model_encryption_key=SecretStr("evidence-test")),
            database=FakeDependency(),
            redis=FakeDependency(),
            qdrant=FakeDependency(),
            llm_provider=provider,
            agent_repository=service.tasks,
            knowledge_repository=KnowledgeRepository(library.sessions),
        )
        app.state.evidence_library = library
        base = "/workbench/projects/research-project/evidence"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(base)
            assert response.status_code == 200, response.text
            rows = response.json()["items"]
            assert len(rows) == 2 and all(row["sources"] for row in rows)
            claim = rows[0]["id"]
            assert (await client.get(base, params={"q": "nonexistent"})).json()["total"] == 0
            assert (await client.get(base, params={"source_type": "web"})).json()["total"] == 1
            assert (await client.get(base, params={"limit": 51})).status_code == 422
            assert (
                await client.get(f"/workbench/projects/other-project/evidence/items/{claim}")
            ).status_code == 404
            assert (
                await client.post(f"/workbench/projects/other-project/evidence/{claim}/disable")
            ).status_code == 404
            assert (
                await client.put(base + "/policy", json={"web_days": 10, "paper_days": 60})
            ).json() == {"web_days": 10, "paper_days": 60}
            assert (
                await client.put(base + "/policy", json={"web_days": 0, "paper_days": 60})
            ).status_code == 422
            assert (await client.get(base + "/policy")).json()["web_days"] == 10
            assert (await client.post(base + f"/{claim}/disable")).status_code == 200
            assert (await client.get(base, params={"status": "withdrawn"})).json()["total"] == 1
            assert (await client.post(base + f"/{claim}/enable")).status_code == 200
            assert (await client.get(f"/workbench/tasks/{task_id}/evidence-reuse")).json()[
                "enabled"
            ] is False
            assert (await client.get("/workbench/projects/missing/evidence")).status_code == 404
        assert len(provider.calls) == before
    finally:
        await engine.dispose()


def test_old_snapshots_keep_reuse_disabled() -> None:
    snapshot = Snapshot(task_id="legacy", objective="old task")
    payload = snapshot.model_dump(
        mode="json", exclude={"evidence_context", "reuse_project_evidence"}
    )
    loaded = Snapshot.model_validate(copy.deepcopy(payload))
    assert loaded.reuse_project_evidence is False and not loaded.evidence_context.claims


async def test_withdrawal_after_frozen_selection_stops_before_next_model(tmp_path: Path) -> None:
    service, engine, library, _ = await published(tmp_path)
    try:
        objective = "Compare LoRA adaptation methods"
        provider = ReusingProvider(objective)
        service.provider.provider = provider
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            dict(
                objective=objective,
                workflow_kind="comparison",
                project_id="research-project",
                research_depth="standard",
                reuse_project_evidence=True,
            ),
            "withdraw-before-plan",
        )
        assert await durable.tick(task_id)  # Select and freeze, without a model call.
        frozen, job = await durable.repository.snapshot(task_id)
        assert frozen.stage == "planner" and frozen.evidence_context.claims and not provider.calls
        await library.set_enabled("research-project", frozen.evidence_context.claims[0].id, False)
        assert await durable.tick(task_id)
        _, job = await durable.repository.snapshot(task_id)
        assert job.status == "failed" and job.error_code == "project_evidence_revoked"
        assert not provider.calls and not job.pending_calls
        async with library.sessions() as session:
            receipts = list(
                await session.scalars(
                    select(EvidenceReuseRow).where(EvidenceReuseRow.task_id == task_id)
                )
            )
            assert receipts and all(row.status == "excluded" for row in receipts)
    finally:
        await engine.dispose()


async def test_evidence_local_only_loader_does_not_change_document_loading_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def loader(**kwargs: object) -> object:
        calls.append(bool(kwargs["local_files_only"]))
        return object()

    monkeypatch.setattr("autoscholar.rag.embeddings.TextEmbedding", loader)
    document = FastEmbedProvider(model="fixture", dimensions=3, cache_dir=tmp_path)
    evidence = document.cached_copy()
    await evidence.require_cached_model()
    assert document._embedding is None
    await document._get_embedding()
    assert calls == [True, False]


async def test_long_pause_cannot_keep_expired_sources_fresh(tmp_path: Path) -> None:
    _, engine, library, _ = await published(tmp_path)
    try:
        selected = await library.select(
            "research-project", "LoRA adaptation", document_ids=[], source_types=["web", "paper"]
        )
        assert selected.claims and all(source.fresh for source in selected.sources)
        frozen = selected.model_dump(mode="json")
        with pytest.raises(AppError) as error:
            await library.validate(selected, [], now=datetime.now(UTC) + timedelta(days=40))
        assert error.value.code == "project_evidence_expired"
        assert selected.model_dump(mode="json") == frozen
    finally:
        await engine.dispose()


async def test_interrupted_local_selection_reconciles_without_replaying_models(
    tmp_path: Path,
) -> None:
    service, engine, library, _ = await published(tmp_path)
    try:
        objective = "Compare LoRA adaptation methods"
        provider = ReusingProvider(objective)
        service.provider.provider = provider
        durable = DurableService(service)
        task_id, _ = await durable.submit(
            dict(
                objective=objective,
                workflow_kind="comparison",
                project_id="research-project",
                research_depth="standard",
                reuse_project_evidence=True,
            ),
            "interrupted-local-selection",
        )
        claimed = await durable.repository.claim(durable.owner, 30, task_id)
        assert claimed
        snapshot, job = await durable.repository.snapshot(task_id)
        await durable.repository.begin(
            task_id, durable.owner, claimed[1], {"stage": "evidence", "version": 1}
        )
        run = snapshot.restore(job.usage, job.active_seconds)
        assert await durable._unit(run, "evidence") == "planner"
        async with library.sessions() as session:
            row = await session.get(WorkflowJobRow, task_id)
            assert row
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        assert await DurableService(service).tick(task_id)
        assert not provider.calls
        from tests.test_research_quality import finish

        recovered, job = await finish(service, task_id)
        assert job.status == "succeeded", job.error_code
        assert recovered.evidence_context.claims and job.usage.get("search_queries", 0) == 0
        assert job.usage["model_calls"] == len(provider.calls) == 9
    finally:
        await engine.dispose()
