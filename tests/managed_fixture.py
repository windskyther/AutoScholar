"""Named-model registry fixture for HTTP serialization tests with in-memory backends.

Real encrypted registry/routing behavior is tested against SQLite in test_model_profiles.
"""

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autoscholar.llm.pricing import ModelPricingRow
from autoscholar.llm.profiles import ModelProfileRow, ModelProfiles
from tests.governance import offline_app

MODEL_ID = "11111111-1111-4111-8111-111111111111"


class ModelSession:
    async def __aenter__(self) -> "ModelSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def scalar(self, statement: object) -> ModelProfileRow:
        return ModelProfileRow(
            id=MODEL_ID,
            name="Managed fixture",
            model="fixture",
            base_url="https://fixture.example/v1",
            compatibility_profile="auto",
            encrypted_key="unit-fixture",
            enabled=True,
            created_at=datetime.now(UTC),
        )

    async def scalars(self, statement: object) -> list[ModelProfileRow]:
        descriptions = getattr(statement, "column_descriptions", [])
        if any(item.get("entity") is ModelPricingRow for item in descriptions):
            return []
        return [await self.scalar(statement)]


def managed_offline_app(*args: Any, **kwargs: Any) -> FastAPI:
    app = offline_app(*args, **kwargs)
    if app.state.model_profiles is None:
        sessions = cast(async_sessionmaker[AsyncSession], ModelSession)
        app.state.model_profiles = ModelProfiles(sessions, app.state.settings)
    return app
