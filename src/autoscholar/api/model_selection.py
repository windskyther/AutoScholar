"""Explicit model selection for all synchronous model endpoints."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import select

from autoscholar.core.errors import AppError
from autoscholar.llm.profiles import ModelProfileRow, current_model_profile


@asynccontextmanager
async def selected_model(request: Request, profile_id: str | None) -> AsyncIterator[None]:
    if not profile_id:
        raise AppError(
            status_code=422, code="model_selection_required", message="Select a managed model"
        )
    profiles = request.app.state.model_profiles
    if profiles is None:
        raise AppError(
            status_code=503, code="workbench_store_unavailable", message="Model storage unavailable"
        )
    async with profiles.sessions() as session:
        row = await session.scalar(
            select(ModelProfileRow).where(
                ModelProfileRow.id == profile_id, ModelProfileRow.enabled.is_(True)
            )
        )
        if row is None:
            raise AppError(
                status_code=422, code="model_profile_unavailable", message="Model unavailable"
            )
    token = current_model_profile.set(profile_id)
    try:
        yield
    finally:
        current_model_profile.reset(token)
