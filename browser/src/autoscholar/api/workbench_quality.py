"""Read-only, bounded quality audit projection, without raw prompts or internal IDs."""

from typing import Any

from sqlalchemy import select

from autoscholar.api.workbench_repository import WorkbenchRepository
from autoscholar.api.workbench_resources import safe_url
from autoscholar.orchestration.durable_models import WorkflowCheckpointRow, WorkflowJobRow
from autoscholar.research.quality_models import QualityState


async def quality_view(repository: WorkbenchRepository, task_id: str) -> dict[str, Any]:
    async with repository.sessions() as session:
        await repository.root(session, task_id)
        job = await session.get(WorkflowJobRow, task_id)
        checkpoint = (
            await session.scalar(
                select(WorkflowCheckpointRow).where(
                    WorkflowCheckpointRow.task_id == task_id,
                    WorkflowCheckpointRow.sequence == job.checkpoint_sequence,
                )
            )
            if job
            else None
        )
        payload = checkpoint.payload if checkpoint else {}
        state = QualityState.model_validate(payload.get("quality", {}))
        initial = {item.source_id: item.model_dump() for item in state.initial}
        refined = {item.source_id: item.model_dump() for item in state.assessments}
        return {
            "depth": payload.get("research_depth", "quick"),
            "phase": state.phase,
            "accepted": state.accepted,
            "reads": state.reads,
            "rounds": state.rounds,
            "supplemental_queries": state.supplemental_queries,
            "sources": [
                {
                    "reference": item.id,
                    "title": item.title,
                    "url": safe_url(item.url),
                    "published_at": item.published_at,
                    "availability": item.availability,
                    "truncated": item.truncated,
                    "read_attempted": item.read_attempted,
                    "initial": initial.get(item.id),
                    "refined": refined.get(item.id),
                }
                for item in state.materials
            ],
            "reviews": [
                {"round": item["round"], "decision": item["decision"], "issues": item["issues"]}
                for item in state.review_history
            ],
        }
