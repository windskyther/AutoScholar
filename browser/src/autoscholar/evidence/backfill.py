"""Explicit, paginated historical import. It never modifies old task outcomes."""

from typing import Any

from sqlalchemy import or_, select

from autoscholar.agent.database_models import AgentTaskRow, EvidenceRow
from autoscholar.evidence.database import EvidencePublicationRow, ProjectClaimRow
from autoscholar.evidence.models import content_hash
from autoscholar.evidence.store import EvidenceStore, deleted_task, enqueue_publication
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow
from autoscholar.research.quality_models import Citation, Claim, Material


async def backfill(
    store: EvidenceStore, project_id: str, *, after: str | None = None, limit: int = 50
) -> dict[str, Any]:
    queued = pending = skipped = 0
    async with store.sessions() as session, session.begin():
        await store.project(session, project_id, lock=True)
        query = select(AgentTaskRow).where(
            AgentTaskRow.project_id == project_id,
            AgentTaskRow.parent_task_id.is_(None),
            AgentTaskRow.status == "succeeded",
            ~deleted_task(AgentTaskRow.id),
        )
        if after:
            query = query.where(AgentTaskRow.id > after)
        roots = list(await session.scalars(query.order_by(AgentTaskRow.id).limit(limit + 1)))
        for root in roots[:limit]:
            publication = await session.get(EvidencePublicationRow, root.id)
            if publication and publication.status == "failed":
                # An explicit local import can retry a bounded exhausted outbox.
                # The publisher still revalidates the original checkpoint/audit.
                publication.status, publication.attempts, publication.error_code = (
                    "pending",
                    0,
                    None,
                )
                queued += 1
                continue
            if publication or await session.scalar(
                select(ProjectClaimRow.id).where(ProjectClaimRow.task_id == root.id).limit(1)
            ):
                skipped += 1
                continue
            job = await session.get(WorkflowJobRow, root.id)
            checkpoint = (
                await session.scalar(
                    select(WorkflowCheckpointRow).where(
                        WorkflowCheckpointRow.task_id == root.id,
                        WorkflowCheckpointRow.sequence == job.checkpoint_sequence,
                    )
                )
                if job
                else None
            )
            if (
                checkpoint
                and job
                and job.status == "succeeded"
                and not job.pending_calls
                and content_hash(checkpoint.payload) == checkpoint.sha256
            ):
                from autoscholar.orchestration.checkpoints import Snapshot

                try:
                    snapshot = Snapshot.model_validate(checkpoint.payload)
                except ValueError:
                    skipped += 1
                    continue
                if (
                    snapshot.quality.accepted
                    and snapshot.quality.sections
                    and snapshot.review
                    and snapshot.review.status == "PASS"
                ):
                    await enqueue_publication(session, snapshot, job.checkpoint_sequence)
                    queued += 1
                    continue
            family = select(AgentTaskRow.id).where(
                AgentTaskRow.project_id == project_id,
                or_(AgentTaskRow.id == root.id, AgentTaskRow.parent_task_id == root.id),
            )
            records = list(
                await session.scalars(
                    select(EvidenceRow)
                    .where(EvidenceRow.task_id.in_(family))
                    .order_by(EvidenceRow.created_at, EvidenceRow.id)
                    .limit(12)
                )
            )
            ordinal = 0
            for record in records:
                try:
                    material = Material.model_validate(
                        dict(
                            id="E1",
                            identity=record.external_id or record.url,
                            kind=record.source_type,
                            title=record.title[:500],
                            url=record.url,
                            text=record.excerpt[:12000],
                            truncated=len(record.excerpt) > 12000,
                            document_id=record.document_id,
                            chunk_id=record.chunk_id,
                            page=record.page,
                            availability="document_excerpt"
                            if record.source_type == "document"
                            else "summary_only",
                        )
                    )
                    if not material.text.strip():
                        continue
                    await store._claim(
                        session,
                        root,
                        ordinal,
                        Claim(
                            text=(record.claim or record.title)[:2400],
                            citations=[Citation(source_id="E1", quote=material.text[:1200])],
                        ),
                        {"E1": material},
                        audit_hash=None,
                        status="pending",
                    )
                    ordinal += 1
                except ValueError:
                    continue
            pending += ordinal
    return {
        "queued_tasks": queued,
        "pending_claims": pending,
        "skipped_tasks": skipped,
        "next_cursor": roots[limit - 1].id if len(roots) > limit else None,
    }
