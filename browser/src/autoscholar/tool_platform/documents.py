"""Read only indexed document chunks after validating a task-bound document grant."""

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.agent.database_models import AgentTaskRow
from autoscholar.rag.database_models import DocumentChunkRow, DocumentRow

DOCUMENT_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {"type": "string", "minLength": 1, "maxLength": 36},
        "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
    },
    "required": ["document_id", "offset", "limit"],
    "additionalProperties": False,
}


class DocumentReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def read(self, context: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        scope = context.get("scope")
        if not isinstance(scope, dict) or arguments["document_id"] not in scope.get(
            "document_ids", []
        ):
            raise PermissionError("Document is outside the task grant")
        async with self.sessions() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION READ ONLY"))
            task = await session.get(AgentTaskRow, scope.get("task_id"))
            if task is None or not task.project_id or task.project_id != scope.get("project_id"):
                raise PermissionError("Task/project scope mismatch")
            document = await session.scalar(
                select(DocumentRow).where(
                    DocumentRow.id == arguments["document_id"],
                    DocumentRow.project_id == task.project_id,
                    DocumentRow.status == "ready",
                )
            )
            if document is None:
                raise PermissionError("Document unavailable in this project")
            chunks = (
                await session.scalars(
                    select(DocumentChunkRow)
                    .where(
                        DocumentChunkRow.document_id == document.id,
                        DocumentChunkRow.project_id == task.project_id,
                    )
                    .order_by(DocumentChunkRow.ordinal)
                    .offset(arguments["offset"])
                    .limit(arguments["limit"] + 1)
                )
            ).all()
            selected = chunks[: arguments["limit"]]
            return {
                "document_id": document.id,
                "project_id": task.project_id,
                "title": document.title,
                "chunks": [
                    {
                        "chunk_id": row.id,
                        "page": row.page,
                        "section": row.section,
                        "content": row.content[:16384],
                        "truncated": len(row.content) > 16384,
                    }
                    for row in selected
                ],
                "next_offset": arguments["offset"] + len(selected)
                if len(chunks) > len(selected)
                else None,
            }
