import time
from typing import Annotated

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, StringConstraints

from autoscholar.llm import ChatMessage, LLMProvider

router = APIRouter(tags=["chat"])
logger = structlog.get_logger(__name__)
MessageText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]


class ChatRequest(BaseModel):
    message: MessageText


class TokenUsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    answer: str
    model: str
    request_id: str
    usage: TokenUsageResponse | None


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    provider: LLMProvider = request.app.state.llm_provider
    started_at = time.perf_counter()
    result = await provider.generate([ChatMessage(role="user", content=payload.message)])
    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

    usage = None
    if result.usage is not None:
        usage = TokenUsageResponse(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            total_tokens=result.usage.total_tokens,
        )

    logger.info(
        "llm_call_completed",
        model=result.model,
        duration_ms=duration_ms,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
    )
    return ChatResponse(
        answer=result.text,
        model=result.model,
        request_id=request.state.request_id,
        usage=usage,
    )
