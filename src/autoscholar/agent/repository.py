from collections.abc import Sequence
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from autoscholar.agent.database_models import AgentTaskRow, EvidenceRow, ToolCallRow
from autoscholar.agent.records import (
    AgentTaskRecord,
    CitationRecord,
    EvidenceRecord,
    ResearchWarningRecord,
    ResolvedAgentMode,
    TaskStatus,
    ToolCallStatus,
    ToolTraceRecord,
)


class AgentTaskRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create_task(
        self, *, task_id: str, objective: str, project_id: str | None = None
    ) -> AgentTaskRecord:
        async with self._sessions() as session:
            row = AgentTaskRow(
                id=task_id,
                project_id=project_id,
                status="running",
                objective=objective,
                plan=[],
                metrics={},
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._task_record(row, ())

    async def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        plan: list[str],
        answer: str | None,
        metrics: dict[str, int],
        error_code: str | None = None,
        error_message: str | None = None,
        mode: ResolvedAgentMode = "compute",
        citations: list[CitationRecord] | None = None,
        warnings: list[ResearchWarningRecord] | None = None,
    ) -> AgentTaskRecord:
        async with self._sessions() as session:
            row = await session.get(AgentTaskRow, task_id)
            if row is None:
                raise LookupError(f"Unknown agent task: {task_id}")
            row.status = status
            row.plan = plan
            row.answer = answer
            row.metrics = metrics
            row.error_code = error_code
            row.error_message = error_message
            row.mode = mode
            row.citations = [
                {"claim": item.claim, "evidence_ids": list(item.evidence_ids)}
                for item in (citations or [])
            ]
            row.warnings = [
                {"code": item.code, "message": item.message, "provider": item.provider}
                for item in (warnings or [])
            ]
            await session.commit()
        record = await self.get_task(task_id)
        if record is None:  # pragma: no cover - protected by the transaction above
            raise LookupError(f"Unknown agent task: {task_id}")
        return record

    async def add_tool_call(
        self,
        *,
        task_id: str,
        sequence: int,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        status: ToolCallStatus,
        duration_ms: float,
        error_code: str | None = None,
    ) -> ToolTraceRecord:
        async with self._sessions() as session:
            row = ToolCallRow(
                id=str(uuid4()),
                task_id=task_id,
                sequence=sequence,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                output=output,
                status=status,
                error_code=error_code,
                duration_ms=duration_ms,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._tool_record(row)

    async def get_task(self, task_id: str) -> AgentTaskRecord | None:
        async with self._sessions() as session:
            result = await session.execute(
                select(AgentTaskRow)
                .where(AgentTaskRow.id == task_id)
                .options(
                    selectinload(AgentTaskRow.tool_calls),
                    selectinload(AgentTaskRow.evidence),
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._task_record(row, row.tool_calls, row.evidence)

    async def add_evidence(
        self,
        *,
        task_id: str,
        citation_key: str,
        source_type: str,
        provider: str,
        title: str,
        url: str,
        authors: tuple[str, ...],
        year: int | None,
        external_id: str | None,
        query: str,
        topic: str,
        claim: str,
        excerpt: str,
        relevance: float,
        document_id: str | None = None,
        chunk_id: str | None = None,
        page: int | None = None,
        section: str | None = None,
    ) -> EvidenceRecord:
        async with self._sessions() as session:
            row = EvidenceRow(
                id=str(uuid4()),
                task_id=task_id,
                citation_key=citation_key,
                source_type=source_type,
                provider=provider,
                title=title,
                url=url,
                authors=list(authors),
                year=year,
                external_id=external_id,
                query=query,
                topic=topic,
                claim=claim,
                excerpt=excerpt,
                relevance=relevance,
                document_id=document_id,
                chunk_id=chunk_id,
                page=page,
                section=section,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._evidence_record(row)

    async def list_evidence(
        self, task_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[EvidenceRecord], int]:
        async with self._sessions() as session:
            total_rows = await session.execute(
                select(EvidenceRow.id).where(EvidenceRow.task_id == task_id)
            )
            total = len(total_rows.scalars().all())
            result = await session.execute(
                select(EvidenceRow)
                .where(EvidenceRow.task_id == task_id)
                .order_by(func.length(EvidenceRow.citation_key), EvidenceRow.citation_key)
                .limit(limit)
                .offset(offset)
            )
            return [self._evidence_record(row) for row in result.scalars().all()], total

    @classmethod
    def _task_record(
        cls,
        row: AgentTaskRow,
        tool_calls: Sequence[ToolCallRow],
        evidence: Sequence[EvidenceRow] = (),
    ) -> AgentTaskRecord:
        return AgentTaskRecord(
            id=row.id,
            status=cast(TaskStatus, row.status),
            objective=row.objective,
            plan=list(row.plan),
            answer=row.answer,
            metrics=dict(row.metrics),
            error_code=row.error_code,
            error_message=row.error_message,
            mode=cast(ResolvedAgentMode, row.mode),
            citations=[
                CitationRecord(
                    claim=str(item["claim"]),
                    evidence_ids=tuple(str(value) for value in item.get("evidence_ids", [])),
                )
                for item in row.citations
            ],
            warnings=[
                ResearchWarningRecord(
                    code=str(item["code"]),
                    message=str(item["message"]),
                    provider=(str(item["provider"]) if item.get("provider") else None),
                )
                for item in row.warnings
            ],
            evidence=[cls._evidence_record(item) for item in evidence],
            created_at=row.created_at,
            updated_at=row.updated_at,
            tool_calls=[cls._tool_record(call) for call in tool_calls],
            project_id=row.project_id,
        )

    @staticmethod
    def _evidence_record(row: EvidenceRow) -> EvidenceRecord:
        return EvidenceRecord(
            id=row.id,
            task_id=row.task_id,
            citation_key=row.citation_key,
            source_type=cast(Literal["web", "paper", "document"], row.source_type),
            provider=row.provider,
            title=row.title,
            url=row.url,
            authors=tuple(row.authors),
            year=row.year,
            external_id=row.external_id,
            query=row.query,
            topic=row.topic,
            claim=row.claim,
            excerpt=row.excerpt,
            relevance=row.relevance,
            created_at=row.created_at,
            document_id=row.document_id,
            chunk_id=row.chunk_id,
            page=row.page,
            section=row.section,
        )

    @staticmethod
    def _tool_record(row: ToolCallRow) -> ToolTraceRecord:
        return ToolTraceRecord(
            id=row.id,
            task_id=row.task_id,
            sequence=row.sequence,
            call_id=row.call_id,
            tool_name=row.tool_name,
            arguments=dict(row.arguments),
            output=row.output,
            status=cast(ToolCallStatus, row.status),
            error_code=row.error_code,
            duration_ms=row.duration_ms,
            created_at=row.created_at,
        )
