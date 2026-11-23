"""Transactional publishing and provenance checks. No LLM or network dependency."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from autoscholar.agent.database_models import AgentTaskRow, EvidenceRow
from autoscholar.core.errors import AppError
from autoscholar.evidence.database import (
    EvidencePublicationRow,
    EvidenceReuseRow,
    ProjectClaimRow,
    SourceVersionRow,
)
from autoscholar.evidence.models import content_hash
from autoscholar.orchestration.durable_models import (
    WorkflowCheckpointRow,
    WorkflowEventRow,
    WorkflowJobRow,
)
from autoscholar.rag.database_models import DocumentRow, ProjectRow
from autoscholar.research.materials import public_url
from autoscholar.research.quality_models import Claim, Material

if TYPE_CHECKING:
    from autoscholar.orchestration.checkpoints import Snapshot


def deleted_task(task_id: Any) -> ColumnElement[bool]:
    return exists().where(
        WorkflowEventRow.task_id == task_id, WorkflowEventRow.kind == "task_deleted"
    )


def version_payload(material: Material) -> dict[str, Any]:
    return material.model_dump(
        exclude={"id", "read_attempted", "bank_claim_ids", "fetched_at", "topics"}
    )


async def enqueue_publication(session: AsyncSession, snapshot: Snapshot, sequence: int) -> None:
    """Called in the transaction which commits the successful task checkpoint."""
    if (
        not snapshot.project_id
        or not snapshot.quality.accepted
        or not snapshot.review
        or snapshot.review.status != "PASS"
        or not snapshot.quality.sections
    ):
        return
    if await session.get(EvidencePublicationRow, snapshot.task_id) is None:
        session.add(
            EvidencePublicationRow(
                task_id=snapshot.task_id,
                project_id=snapshot.project_id,
                checkpoint_sequence=sequence,
                audit_hash=snapshot.quality.bundle_sha256,
                status="pending",
                attempts=0,
            )
        )


async def exclude_unfinished_reuse(session: AsyncSession, task_id: str) -> None:
    await session.execute(
        update(EvidenceReuseRow)
        .where(EvidenceReuseRow.task_id == task_id, EvidenceReuseRow.status == "selected")
        .values(status="excluded", reason="Task stopped before a successful final report")
    )


async def finish_reuse(session: AsyncSession, snapshot: Snapshot) -> None:
    used_sources = {
        citation.source_id
        for section in snapshot.quality.sections
        for claim in section.claims
        for citation in claim.citations
    }
    adopted = {
        parent
        for material in snapshot.quality.materials
        if material.id in used_sources
        for parent in material.bank_claim_ids
    }
    if snapshot.research_depth == "quick":
        # The quick Writer includes each current child's selected evidence.
        # Bind adoption to its stored bank source version, never to model prose.
        references = [
            item.get("reference", "")
            for result in snapshot.results.values()
            for item in result.get("evidence", [])
        ]
        cited_versions = set()
        for reference in references:
            child_id, _, citation_key = reference.rpartition(":")
            record = await session.scalar(
                select(EvidenceRow).where(
                    EvidenceRow.task_id == child_id,
                    EvidenceRow.citation_key == citation_key,
                    EvidenceRow.provider == "project_evidence",
                )
            )
            if record and record.external_id:
                cited_versions.add(record.external_id)
        adopted = {
            claim.id
            for claim in snapshot.evidence_context.claims
            if any(link["source_id"] in cited_versions for link in claim.supports)
        }
    for claim in snapshot.evidence_context.claims:
        key = str(uuid5(NAMESPACE_URL, f"autoscholar:reuse:{snapshot.task_id}:{claim.id}"))
        reuse = await session.get(EvidenceReuseRow, key)
        if reuse:
            reuse.status = "adopted" if claim.id in adopted else "excluded"
            reuse.reason = (
                "Used original source passages"
                if claim.id in adopted
                else "Not cited after current-task screening"
            )


class EvidenceStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    async def project(session: AsyncSession, project_id: str, *, lock: bool = False) -> ProjectRow:
        query = select(ProjectRow).where(
            ProjectRow.id == project_id, ProjectRow.deleted_at.is_(None)
        )
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise AppError(
                status_code=404, code="project_not_found", message="Project was not found"
            )
        return row

    @staticmethod
    async def visible_task(
        session: AsyncSession, task_id: str, project_id: str
    ) -> AgentTaskRow | None:
        row: AgentTaskRow | None = await session.scalar(
            select(AgentTaskRow).where(
                AgentTaskRow.id == task_id,
                AgentTaskRow.project_id == project_id,
                AgentTaskRow.parent_task_id.is_(None),
                AgentTaskRow.status == "succeeded",
                ~deleted_task(task_id),
            )
        )
        return row

    @staticmethod
    async def source_valid(
        session: AsyncSession,
        source: SourceVersionRow,
        project_id: str,
        document_ids: list[str] | None = None,
    ) -> bool:
        if source.project_id != project_id or content_hash(source.material) != source.content_hash:
            return False
        try:
            material = Material.model_validate({"id": "E1", **source.material})
        except ValueError:
            return False
        if material.kind != "document":
            return public_url(material.url)
        if not material.document_id or document_ids == []:
            return False
        if document_ids is not None and material.document_id not in document_ids:
            return False
        document = await session.get(DocumentRow, material.document_id)
        return bool(
            document
            and document.project_id == project_id
            and document.status == "ready"
            and document.sha256 == source.document_sha256
        )

    async def valid_claim(
        self,
        session: AsyncSession,
        claim: ProjectClaimRow,
        *,
        project_id: str,
        document_ids: list[str] | None = None,
        path: frozenset[str] = frozenset(),
        memo: dict[str, bool] | None = None,
    ) -> bool:
        memo = {} if memo is None else memo
        if claim.id in memo:
            return memo[claim.id]
        memo[claim.id] = False
        project = await session.get(ProjectRow, project_id)
        if project is None or project.deleted_at is not None:
            return False
        if (
            claim.id in path
            or len(path) >= 32
            or claim.project_id != project_id
            or not claim.enabled
            or claim.review_status != "ready"
            or await self.visible_task(session, claim.task_id, project_id) is None
        ):
            return False
        if not claim.supports or len(claim.supports) > 6 or len(claim.dependencies) > 40:
            return False
        for link in claim.supports:
            source = await session.get(SourceVersionRow, link.get("source_id", ""))
            if (
                source is None
                or not await self.source_valid(session, source, project_id, document_ids)
                or not link.get("quote", "").strip()
                or link["quote"] not in str(source.material.get("text", ""))
            ):
                return False
        for dependency_id in claim.dependencies:
            dependency = await session.get(ProjectClaimRow, dependency_id)
            if dependency is None or not await self.valid_claim(
                session,
                dependency,
                project_id=project_id,
                document_ids=document_ids,
                path=path | {claim.id},
                memo=memo,
            ):
                return False
        memo[claim.id] = True
        return True

    async def _source(
        self, session: AsyncSession, project_id: str, material: Material
    ) -> SourceVersionRow:
        payload = version_payload(material)
        identity_hash, digest = content_hash(material.identity), content_hash(payload)
        key = str(uuid5(NAMESPACE_URL, f"autoscholar:source:{project_id}:{identity_hash}:{digest}"))
        row = await session.get(SourceVersionRow, key)
        if row is None:
            document_sha = None
            if material.kind == "document":
                document = await session.get(DocumentRow, material.document_id)
                if not document or document.project_id != project_id or document.status != "ready":
                    raise ValueError("Source document is not available in the project")
                document_sha = document.sha256
            elif not public_url(material.url):
                raise ValueError("Public source URL is invalid")
            row = SourceVersionRow(
                id=key,
                project_id=project_id,
                identity_hash=identity_hash,
                content_hash=digest,
                material=payload,
                document_sha256=document_sha,
                fetched_at=material.fetched_at,
            )
            session.add(row)
            await session.flush()
        elif material.fetched_at and not material.bank_claim_ids:
            # Only an actual new acquisition refreshes the check time. Library
            # reuse carries its original timestamp instead of minting freshness.
            from datetime import UTC

            previous = row.fetched_at
            if previous is None or material.fetched_at > previous.replace(tzinfo=UTC):
                row.fetched_at = material.fetched_at
        return row

    async def _claim(
        self,
        session: AsyncSession,
        task: AgentTaskRow,
        ordinal: int,
        claim: Claim,
        materials: dict[str, Material],
        *,
        audit_hash: str | None,
        status: str = "ready",
    ) -> None:
        key = str(uuid5(NAMESPACE_URL, f"autoscholar:claim:{task.id}:{ordinal}"))
        if await session.get(ProjectClaimRow, key):
            return
        assert task.project_id
        supports: list[dict[str, str]] = []
        dependencies: set[str] = set()
        for citation in claim.citations:
            source = materials.get(citation.source_id)
            if source is None or citation.quote not in source.text or not citation.quote.strip():
                raise ValueError("Claim quotations must match the frozen original material")
            version = await self._source(session, task.project_id, source)
            supports.append({"source_id": version.id, "quote": citation.quote})
            dependencies.update(getattr(source, "bank_claim_ids", []))
        for dependency_id in dependencies:
            parent = await session.get(ProjectClaimRow, dependency_id)
            if (
                parent is None
                or parent.task_id == task.id
                or not await self.valid_claim(session, parent, project_id=task.project_id)
            ):
                raise ValueError("A reused claim was withdrawn or has invalid provenance")
        session.add(
            ProjectClaimRow(
                id=key,
                project_id=task.project_id,
                task_id=task.id,
                ordinal=ordinal,
                text=claim.text,
                topic=task.objective,
                source_types="|"
                + "|".join(sorted({materials[c.source_id].kind for c in claim.citations}))
                + "|",
                supports=supports,
                dependencies=sorted(dependencies),
                review_status=status,
                enabled=True,
                audit_hash=audit_hash,
                indexed_version=0,
            )
        )

    async def publish_one(self) -> bool:
        """Project lock comes before the outbox row; deletion uses the same order."""
        async with self.sessions() as session:
            candidate = (
                await session.execute(
                    select(
                        EvidencePublicationRow.task_id,
                        EvidencePublicationRow.project_id,
                    )
                    .where(
                        EvidencePublicationRow.status == "pending",
                        EvidencePublicationRow.attempts < 5,
                    )
                    .order_by(EvidencePublicationRow.created_at)
                    .limit(1)
                )
            ).first()
        if candidate is None:
            return False
        task_id, project_id = candidate
        try:
            async with self.sessions() as session, session.begin():
                project = await session.scalar(
                    select(ProjectRow).where(ProjectRow.id == project_id).with_for_update()
                )
                publication = await session.scalar(
                    select(EvidencePublicationRow)
                    .where(
                        EvidencePublicationRow.task_id == task_id,
                        EvidencePublicationRow.status == "pending",
                    )
                    .with_for_update(skip_locked=True)
                )
                if publication is None:
                    return False
                task = await self.visible_task(session, task_id, project_id)
                if project is None or project.deleted_at or task is None:
                    publication.status = "withdrawn"
                    return True
                job = await session.get(WorkflowJobRow, task_id)
                if job is None or job.status != "succeeded" or job.pending_calls:
                    raise ValueError("Task does not have a settled successful workflow")
                checkpoint = await session.scalar(
                    select(WorkflowCheckpointRow).where(
                        WorkflowCheckpointRow.task_id == task_id,
                        WorkflowCheckpointRow.sequence == publication.checkpoint_sequence,
                    )
                )
                if checkpoint is None or content_hash(checkpoint.payload) != checkpoint.sha256:
                    raise ValueError("Publication checkpoint integrity check failed")
                from autoscholar.orchestration.checkpoints import Snapshot

                snapshot = Snapshot.model_validate(checkpoint.payload)
                state = snapshot.quality
                import hashlib

                if (
                    snapshot.project_id != project_id
                    or not state.accepted
                    or not snapshot.review
                    or snapshot.review.status != "PASS"
                    or state.phase != "done"
                    or state.bundle_sha256 != publication.audit_hash
                    or hashlib.sha256(state.bundle.encode()).hexdigest() != state.bundle_sha256
                ):
                    raise ValueError("Research quality audit is not eligible for publication")
                bound = {
                    item.citation_key: item
                    for item in await session.scalars(
                        select(EvidenceRow).where(
                            EvidenceRow.task_id == task_id,
                            EvidenceRow.provider == "research_quality",
                        )
                    )
                }
                materials = {item.id: item for item in state.materials}
                ordinal = 0
                for section in state.sections:
                    for claim in section.claims:
                        for citation in claim.citations:
                            material, evidence = (
                                materials.get(citation.source_id),
                                bound.get(citation.source_id),
                            )
                            if (
                                material is None
                                or evidence is None
                                or evidence.url != material.url
                                or evidence.excerpt != material.text
                            ):
                                raise ValueError(
                                    "Final report evidence does not match the quality snapshot"
                                )
                        await self._claim(
                            session,
                            task,
                            ordinal,
                            claim,
                            materials,
                            audit_hash=publication.audit_hash,
                        )
                        ordinal += 1
                publication.status, publication.error_code = "done", None
        except Exception as error:
            async with self.sessions() as session, session.begin():
                publication = await session.scalar(
                    select(EvidencePublicationRow)
                    .where(EvidencePublicationRow.task_id == task_id)
                    .with_for_update()
                )
                if publication and publication.status == "pending":
                    publication.attempts += 1
                    publication.error_code = (
                        "evidence_integrity_failed"
                        if isinstance(error, ValueError)
                        else "evidence_store_unavailable"
                    )
                    if publication.attempts >= 5:
                        publication.status = "failed"
        return True
