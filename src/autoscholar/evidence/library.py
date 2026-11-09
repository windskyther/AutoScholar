"""Bounded project lookup, read-only projections and frozen reuse selections."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, func, or_, select, update

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.core.errors import AppError
from autoscholar.evidence.database import (
    EvidencePolicyRow,
    EvidenceReuseRow,
    ProjectClaimRow,
    SourceVersionRow,
)
from autoscholar.evidence.models import BankClaim, BankSource, EvidenceContext, content_hash
from autoscholar.evidence.store import EvidenceStore, deleted_task, version_payload
from autoscholar.research.context import research_context
from autoscholar.research.materials import public_url
from autoscholar.research.quality_models import Material


def tokens(text: str) -> set[str]:
    text = re.sub(
        r"研究|最新|成果|领域|目标|资料|报告|方案|任务|分析|综述|进行|相关|哪些|如何",
        " ",
        text.lower(),
    )
    words = set(re.findall(r"[a-z][a-z0-9_-]+", text))
    for part in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        words.update(part[index : index + 2] for index in range(len(part) - 1))
    return words


def context_hash(context: EvidenceContext) -> str:
    return content_hash(context.model_dump(mode="json", exclude={"snapshot_hash"}))


class EvidenceRevoked(AppError):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            code="project_evidence_revoked",
            message="Selected project evidence changed or was withdrawn",
        )


class EvidenceLibrary(EvidenceStore):
    semantic: Any = None

    async def select(
        self,
        project_id: str,
        objective: str,
        *,
        document_ids: list[str] | None,
        source_types: list[str],
        document_only: bool = False,
        exclude_task_id: str | None = None,
        now: datetime | None = None,
    ) -> EvidenceContext:
        now = now or datetime.now(UTC)
        query_tokens = tokens(objective)
        context = EvidenceContext(project_id=project_id, objective=objective)
        async with self.sessions() as session:
            await self.project(session, project_id)
            policy = await session.get(EvidencePolicyRow, project_id)
            query = (
                select(ProjectClaimRow)
                .join(AgentTaskRow, AgentTaskRow.id == ProjectClaimRow.task_id)
                .where(
                    ProjectClaimRow.project_id == project_id,
                    ProjectClaimRow.enabled.is_(True),
                    ProjectClaimRow.review_status == "ready",
                    AgentTaskRow.project_id == project_id,
                    AgentTaskRow.status == "succeeded",
                    ~deleted_task(AgentTaskRow.id),
                )
            )
            if exclude_task_id:
                query = query.where(ProjectClaimRow.task_id != exclude_task_id)
            rows = list(
                await session.scalars(
                    query.order_by(ProjectClaimRow.created_at.desc(), ProjectClaimRow.id).limit(
                        1000
                    )
                )
            )
            dense: dict[str, float] = {}
            if self.semantic is not None:
                try:
                    dense = await self.semantic.search(project_id, objective, limit=40)
                    if dense:
                        known = {row.id for row in rows}
                        rows.extend(
                            await session.scalars(
                                query.where(
                                    ProjectClaimRow.id.in_(
                                        [key for key in dense if key not in known]
                                    )
                                )
                            )
                        )
                except Exception:
                    context.note = "keyword_fallback"
            ranked = []
            for row in rows:
                overlap = len(query_tokens & tokens(row.text + " " + row.topic))
                semantic_score = dense.get(row.id, 0)
                if overlap or semantic_score >= 0.45:
                    ranked.append((overlap + semantic_score * 3, row))
            ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
            sources: dict[str, BankSource] = {}
            remaining = 24000
            fingerprints: set[str] = set()
            date_scope = research_context(objective, now.date())
            for _, row in ranked:
                fingerprint = content_hash({"text": row.text, "supports": row.supports})
                if fingerprint in fingerprints:
                    continue
                if len(context.claims) >= 12:
                    break
                if not await self.valid_claim(
                    session, row, project_id=project_id, document_ids=document_ids
                ):
                    continue
                collected: list[BankSource] = []
                eligible = True
                for link in row.supports:
                    source = await session.get(SourceVersionRow, link["source_id"])
                    assert source is not None
                    material = Material.model_validate({"id": "E1", **source.material})
                    if (document_only and material.kind != "document") or (
                        material.kind != "document" and material.kind not in source_types
                    ):
                        eligible = False
                        break
                    fetched_at = source.fetched_at
                    if fetched_at and fetched_at.tzinfo is None:
                        fetched_at = fetched_at.replace(tzinfo=UTC)
                    fresh = material.kind == "document" or bool(
                        fetched_at
                        and now
                        - timedelta(
                            days=(policy.web_days if policy else 7)
                            if material.kind == "web"
                            else (policy.paper_days if policy else 30)
                        )
                        <= fetched_at
                        <= now
                    )
                    if date_scope.get("requested_start") and material.kind != "document":
                        fresh &= bool(
                            material.published_at
                            and date_scope["requested_start"]
                            <= material.published_at
                            <= date_scope["requested_end"]
                        )
                    material.fetched_at = fetched_at
                    collected.append(
                        BankSource(
                            version_id=source.id,
                            material=material,
                            document_sha256=source.document_sha256,
                            fetched_at=fetched_at,
                            fresh=fresh,
                        )
                    )
                added = {s.version_id: s for s in collected if s.version_id not in sources}
                size = sum(len(s.material.text) for s in added.values())
                if not eligible or size > remaining or len(sources) + len(added) > 40:
                    continue
                sources.update(added)
                fingerprints.add(fingerprint)
                remaining -= size
                context.claims.append(
                    BankClaim(
                        id=row.id,
                        task_id=row.task_id,
                        objective=row.topic,
                        text=row.text,
                        supports=row.supports,
                        dependencies=row.dependencies,
                    )
                )
            context.sources = list(sources.values())
        context.snapshot_hash = context_hash(context)
        return context

    async def validate(
        self,
        context: EvidenceContext,
        document_ids: list[str] | None,
        *,
        now: datetime | None = None,
    ) -> None:
        if not context.claims:
            return
        if not context.project_id or context.snapshot_hash != context_hash(context):
            raise EvidenceRevoked()
        async with self.sessions() as session:
            await self.project(session, context.project_id)
            policy = await session.get(EvidencePolicyRow, context.project_id)
            now = now or datetime.now(UTC)
            for frozen in context.claims:
                claim = await session.get(ProjectClaimRow, frozen.id)
                if (
                    claim is None
                    or claim.text != frozen.text
                    or claim.supports != frozen.supports
                    or claim.task_id != frozen.task_id
                    or claim.dependencies != frozen.dependencies
                    or not await self.valid_claim(
                        session, claim, project_id=context.project_id, document_ids=document_ids
                    )
                ):
                    raise EvidenceRevoked()
            for frozen_source in context.sources:
                if frozen_source.fresh and frozen_source.material.kind != "document":
                    acquired = frozen_source.fetched_at
                    if acquired and acquired.tzinfo is None:
                        acquired = acquired.replace(tzinfo=UTC)
                    days = (
                        (policy.web_days if policy else 7)
                        if frozen_source.material.kind == "web"
                        else (policy.paper_days if policy else 30)
                    )
                    if acquired is None or not now - timedelta(days=days) <= acquired <= now:
                        raise AppError(
                            status_code=409,
                            code="project_evidence_expired",
                            message="Selected evidence needs a new source recheck",
                        )
                source = await session.get(SourceVersionRow, frozen_source.version_id)
                if (
                    source is None
                    or version_payload(frozen_source.material) != source.material
                    or not await self.source_valid(
                        session, source, context.project_id, document_ids
                    )
                ):
                    raise EvidenceRevoked()

    async def record_selection(
        self, task_id: str, context: EvidenceContext, *, replace_uncheckpointed: bool = False
    ) -> None:
        async with self.sessions() as session, session.begin():
            task = await session.get(AgentTaskRow, task_id)
            if task is None or task.project_id != context.project_id:
                raise EvidenceRevoked()
            if replace_uncheckpointed:
                # The caller is still in the local selection unit. No model has
                # consumed this selection; an interrupted attempt can be replaced.
                await session.execute(
                    delete(EvidenceReuseRow).where(
                        EvidenceReuseRow.task_id == task_id, EvidenceReuseRow.status == "selected"
                    )
                )
            for claim in context.claims:
                key = str(uuid5(NAMESPACE_URL, f"autoscholar:reuse:{task_id}:{claim.id}"))
                old = await session.get(EvidenceReuseRow, key)
                if old and old.snapshot_hash != context.snapshot_hash:
                    raise EvidenceRevoked()
                if old is None:
                    session.add(
                        EvidenceReuseRow(
                            id=key,
                            task_id=task_id,
                            claim_id=claim.id,
                            status="selected",
                            reason="",
                            snapshot_hash=context.snapshot_hash,
                        )
                    )

    async def page(
        self,
        project_id: str,
        *,
        query: str = "",
        claim_id: str | None = None,
        source_type: str | None = None,
        status: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        async with self.sessions() as session:
            await self.project(session, project_id)
            policy = await session.get(EvidencePolicyRow, project_id)
            now = datetime.now(UTC)
            conditions = [
                ProjectClaimRow.project_id == project_id,
                AgentTaskRow.project_id == project_id,
                ~deleted_task(AgentTaskRow.id),
            ]
            if source_type:
                conditions.append(ProjectClaimRow.source_types.contains("|" + source_type + "|"))
            if claim_id:
                conditions.append(ProjectClaimRow.id == claim_id)
            if query:
                pattern = (
                    "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                conditions.append(
                    or_(
                        ProjectClaimRow.text.ilike(pattern, escape="\\"),
                        ProjectClaimRow.topic.ilike(pattern, escape="\\"),
                    )
                )
            if status == "withdrawn":
                conditions.append(ProjectClaimRow.enabled.is_(False))
            elif status == "pending":
                conditions.append(ProjectClaimRow.review_status == "pending")
            elif status == "ready":
                conditions.extend(
                    [ProjectClaimRow.enabled.is_(True), ProjectClaimRow.review_status == "ready"]
                )
            base = (
                select(ProjectClaimRow)
                .join(AgentTaskRow, AgentTaskRow.id == ProjectClaimRow.task_id)
                .where(*conditions)
            )
            total = await session.scalar(select(func.count()).select_from(base.subquery()))
            rows = list(
                await session.scalars(
                    base.order_by(ProjectClaimRow.created_at.desc(), ProjectClaimRow.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            items = []
            for row in rows:
                sources = []
                for link in row.supports:
                    source = await session.get(SourceVersionRow, link["source_id"])
                    if source is None or source.project_id != project_id:
                        continue
                    kind = source.material["kind"]
                    url = source.material.get("url")
                    if kind == "document":
                        doc_id = source.material.get("document_id")
                        url = (
                            f"/api/workbench/projects/{project_id}/documents/{doc_id}/content"
                            if doc_id and await self.source_valid(session, source, project_id)
                            else None
                        )
                    elif not url or not public_url(url):
                        url = None
                    acquired = source.fetched_at
                    if acquired and acquired.tzinfo is None:
                        acquired = acquired.replace(tzinfo=UTC)
                    days = (
                        (policy.web_days if policy else 7)
                        if kind == "web"
                        else (policy.paper_days if policy else 30)
                    )
                    sources.append(
                        {
                            "title": source.material["title"],
                            "url": url,
                            "kind": source.material["kind"],
                            "quote": link["quote"],
                            "published_at": source.material.get("published_at"),
                            "availability": source.material["availability"],
                            "page": source.material.get("page"),
                            "fetched_at": source.fetched_at,
                            "truncated": source.material.get("truncated", False),
                            "recheck_due": kind != "document"
                            and not bool(
                                acquired and now - timedelta(days=days) <= acquired <= now
                            ),
                        }
                    )
                used = await session.scalar(
                    select(func.count())
                    .select_from(EvidenceReuseRow)
                    .where(
                        EvidenceReuseRow.claim_id == row.id, EvidenceReuseRow.status == "adopted"
                    )
                )
                usable = await self.valid_claim(session, row, project_id=project_id)
                items.append(
                    {
                        "id": row.id,
                        "task_id": row.task_id,
                        "text": row.text,
                        "objective": row.topic,
                        "enabled": row.enabled,
                        "status": row.review_status
                        if usable or row.review_status == "pending"
                        else "withdrawn",
                        "sources": sources,
                        "reuse_count": int(used or 0),
                        "created_at": row.created_at,
                    }
                )
            return {"items": items, "total": int(total or 0), "limit": limit, "offset": offset}

    async def set_enabled(self, project_id: str, claim_id: str, enabled: bool) -> None:
        async with self.sessions() as session, session.begin():
            await self.project(session, project_id, lock=True)
            claim = await session.get(ProjectClaimRow, claim_id)
            if claim is None or claim.project_id != project_id:
                raise AppError(
                    status_code=404,
                    code="project_evidence_not_found",
                    message="Evidence was not found",
                )
            if enabled and await self.visible_task(session, claim.task_id, project_id) is None:
                raise EvidenceRevoked()
            claim.enabled = enabled
            if (
                enabled
                and claim.review_status == "ready"
                and not await self.valid_claim(
                    session,
                    claim,
                    project_id=project_id,
                )
            ):
                raise EvidenceRevoked()
            await session.execute(
                update(ProjectClaimRow)
                .where(ProjectClaimRow.project_id == project_id)
                .values(indexed_version=0)
            )

    async def policy(self, project_id: str, values: dict[str, int] | None = None) -> dict[str, int]:
        async with self.sessions() as session, session.begin():
            await self.project(session, project_id, lock=values is not None)
            row = await session.get(EvidencePolicyRow, project_id)
            if values is not None:
                if set(values) != {"web_days", "paper_days"} or not all(
                    1 <= v <= 365 for v in values.values()
                ):
                    raise ValueError("Recheck intervals must be between one and 365 days")
                if row is None:
                    row = EvidencePolicyRow(project_id=project_id, **values)
                    session.add(row)
                else:
                    row.web_days, row.paper_days = values["web_days"], values["paper_days"]
            return {
                "web_days": row.web_days if row else 7,
                "paper_days": row.paper_days if row else 30,
            }
