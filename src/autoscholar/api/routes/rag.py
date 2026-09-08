from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, StringConstraints

from autoscholar.api.routes.agent import (
    CitationResponse,
    EvidenceResponse,
    _citations,
    _evidence,
)
from autoscholar.core.errors import AppError
from autoscholar.rag.models import RetrievalMode
from autoscholar.rag.service import RAGQueryServiceProtocol

router = APIRouter(prefix="/projects", tags=["knowledge"])
QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


class RAGQueryRequest(BaseModel):
    question: QuestionText
    document_ids: list[str] | None = None
    retrieval_mode: RetrievalMode = "hybrid_rerank"
    top_k: int | None = Field(default=None, ge=1, le=50)


class RAGQueryResponse(BaseModel):
    task_id: str
    status: str
    answer: str
    model: str
    retrieval_mode: RetrievalMode
    evidence: list[EvidenceResponse]
    citations: list[CitationResponse]
    request_id: str


@router.post("/{project_id}/rag/query", response_model=RAGQueryResponse)
async def query_knowledge_base(
    project_id: str, payload: RAGQueryRequest, request: Request
) -> RAGQueryResponse:
    service: RAGQueryServiceProtocol | None = request.app.state.rag_query_service
    if service is None:
        raise AppError(
            status_code=503,
            code="rag_not_available",
            message="Knowledge-base querying is unavailable",
        )
    result = await service.query(
        payload.question,
        project_id=project_id,
        document_ids=payload.document_ids,
        retrieval_mode=payload.retrieval_mode,
        top_k=payload.top_k,
    )
    task = result.task
    return RAGQueryResponse(
        task_id=task.id,
        status=task.status,
        answer=task.answer or "",
        model=result.model,
        retrieval_mode=result.retrieval_mode,
        evidence=[_evidence(item) for item in task.evidence],
        citations=_citations(task.citations),
        request_id=request.state.request_id,
    )
