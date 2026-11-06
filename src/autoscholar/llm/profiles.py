"""Named, immutable model connections. Keys are encrypted and never returned to clients."""

import asyncio
import base64
import hashlib
import hmac
from contextvars import ContextVar
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator
from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from autoscholar.agent.database_models import Base
from autoscholar.core.config import Settings
from autoscholar.core.errors import AppError
from autoscholar.core.redaction import configure_redaction
from autoscholar.core.rejections import LocalRequestRejected
from autoscholar.llm.base import LLMProvider
from autoscholar.llm.factory import create_llm_provider
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.llm.preflight import preflight_request
from autoscholar.llm.usage import UsageStore
from autoscholar.tool_platform.context import current_workflow_claim

current_model_profile: ContextVar[str | None] = ContextVar("model_profile", default=None)
TextName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ModelProfileRow(Base):
    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(2048))
    model: Mapped[str] = mapped_column(String(200))
    compatibility_profile: Mapped[str] = mapped_column(String(16))
    encrypted_key: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: TextName
    base_url: str = Field(min_length=1, max_length=2048)
    model: TextName
    api_key: SecretStr
    compatibility_profile: Literal["auto", "generic", "deepseek"] = "auto"

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        try:
            url = httpx.URL(value.strip().rstrip("/"))
            if (
                url.scheme not in {"http", "https"}
                or not url.host
                or url.userinfo
                or url.query
                or url.fragment
                or (
                    url.scheme == "http"
                    and url.host
                    not in {
                        "localhost",
                        "127.0.0.1",
                        "::1",
                        "host.docker.internal",
                    }
                )
                or url.path.endswith("/chat/completions")
            ):
                raise ValueError
            return str(url).rstrip("/")
        except (ValueError, httpx.InvalidURL) as exc:
            raise ValueError("Use an HTTPS API base URL, or HTTP for a local model") from exc

    @field_validator("api_key")
    @classmethod
    def credential(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= 4096 or any(not 33 <= ord(char) <= 126 for char in raw):
            raise ValueError("API key must be a nonempty single-line ASCII credential")
        return value


class ModelProfilePublic(BaseModel):
    id: str
    name: str
    base_url: str
    model: str
    compatibility_profile: Literal["auto", "generic", "deepseek"]
    enabled: bool
    created_at: datetime


def public(row: ModelProfileRow) -> ModelProfilePublic:
    return ModelProfilePublic.model_validate(
        {key: getattr(row, key) for key in ModelProfilePublic.model_fields}
    )


class ModelProfiles:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings
        self.providers: dict[str, LLMProvider] = {}
        self.bindings: dict[str, tuple[str, str]] = {}
        self.usage = UsageStore(sessions)
        self.lock = asyncio.Lock()

    def cipher(self) -> Fernet:
        token = self.settings.experiment_api_token
        if not token:
            raise AppError(
                status_code=503,
                code="model_key_unavailable",
                message="Model credential storage is unavailable",
            )
        key = hashlib.sha256(b"autoscholar-model-keys-v1\0" + token.get_secret_value().encode())
        return Fernet(base64.urlsafe_b64encode(key.digest()))

    async def list(self) -> list[ModelProfilePublic]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(ModelProfileRow).order_by(ModelProfileRow.created_at)
            )
            return [public(row) for row in rows]

    async def create(self, payload: ModelProfileCreate) -> ModelProfilePublic:
        key = payload.api_key.get_secret_value()
        configure_redaction(self.settings.model_copy(update={"llm_api_key": payload.api_key}))
        async with self.sessions() as session:
            # The caller supplies a UUID so an unknown acknowledgement is repeatable.
            existing = await session.get(ModelProfileRow, str(payload.id))
            if existing:
                same = all(
                    getattr(existing, field) == getattr(payload, field)
                    for field in (
                        "name",
                        "base_url",
                        "model",
                        "compatibility_profile",
                    )
                )
                try:
                    same = same and hmac.compare_digest(
                        self.cipher().decrypt(existing.encrypted_key.encode()).decode(),
                        key,
                    )
                except InvalidToken:
                    same = False
                if not same:
                    raise AppError(
                        status_code=409,
                        code="model_profile_conflict",
                        message="Model configuration changed",
                    )
                return public(existing)
            count = await session.scalar(select(func.count()).select_from(ModelProfileRow))
            if (count or 0) >= 100:
                raise AppError(
                    status_code=409,
                    code="model_profile_limit",
                    message="At most 100 model connections",
                )
            row = ModelProfileRow(
                id=str(payload.id),
                name=payload.name,
                base_url=payload.base_url,
                model=payload.model,
                compatibility_profile=payload.compatibility_profile,
                encrypted_key=self.cipher().encrypt(key.encode()).decode(),
                enabled=True,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return public(row)

    async def enabled(self, profile_id: str, enabled: bool) -> ModelProfilePublic:
        async with self.sessions() as session:
            row = await session.scalar(
                select(ModelProfileRow)
                .where(
                    ModelProfileRow.id == profile_id,
                )
                .with_for_update()
            )
            if not row:
                raise AppError(
                    status_code=404,
                    code="model_profile_not_found",
                    message="Model connection not found",
                )
            row.enabled = enabled
            await session.commit()
            await session.refresh(row)
            return public(row)

    async def provider(self, profile_id: str) -> LLMProvider:
        async with self.lock:
            if profile_id not in self.providers:
                async with self.sessions() as session:
                    row = await session.get(ModelProfileRow, profile_id)
                    if not row:
                        raise AppError(
                            status_code=404,
                            code="model_profile_not_found",
                            message="Model connection not found",
                        )
                    try:
                        key = SecretStr(self.cipher().decrypt(row.encrypted_key.encode()).decode())
                    except InvalidToken as exc:
                        raise AppError(
                            status_code=503,
                            code="model_key_unavailable",
                            message="Re-enter model credentials",
                        ) from exc
                    config = self.settings.model_copy(
                        update={
                            "llm_base_url": row.base_url,
                            "llm_model": row.model,
                            "llm_api_key": key,
                            "llm_compatibility_profile": row.compatibility_profile,
                        }
                    )
                    configure_redaction(config)
                    self.providers[profile_id] = create_llm_provider(config)
                    self.bindings[profile_id] = (row.name, row.model)
            return self.providers[profile_id]

    async def binding(self, profile_id: str) -> tuple[str, str]:
        if profile_id not in self.bindings:
            async with self.sessions() as session:
                row = await session.get(ModelProfileRow, profile_id)
                if row is None:
                    raise AppError(
                        status_code=404,
                        code="model_profile_not_found",
                        message="Model connection not found",
                    )
                self.bindings[profile_id] = (row.name, row.model)
        return self.bindings[profile_id]

    async def close(self) -> None:
        for provider in self.providers.values():
            await provider.close()
        self.providers.clear()
        self.bindings.clear()


class RoutedLLM:
    """Route every role in a workflow through its saved connection, isolated per task."""

    def __init__(self, default: LLMProvider, profiles: ModelProfiles) -> None:
        self.default, self.profiles = default, profiles
        self.completion: ContextVar[tuple[str, str, TokenUsage | None] | None] = ContextVar(
            "model_usage_completion",
            default=None,
        )

    @property
    def configured(self) -> bool:
        return current_model_profile.get() is not None or self.default.configured

    def check_available(self) -> None:
        if current_model_profile.get() is None:
            check = getattr(self.default, "check_available", None)
            if callable(check):
                check()

    async def selected(self) -> LLMProvider:
        profile_id = current_model_profile.get()
        return await self.profiles.provider(profile_id) if profile_id else self.default

    async def preflight(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
        reserve_bytes: int = 0,
    ) -> None:
        await preflight_request(
            await self.selected(),
            messages,
            tools=tools,
            tool_choice=tool_choice,
            reserve_bytes=reserve_bytes,
        )

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        from autoscholar.core.budget import current_parent

        provider = await self.selected()
        if not provider.configured:
            return await provider.generate(messages, tools=tools, tool_choice=tool_choice)
        profile_id = current_model_profile.get()
        api_name, model = (
            await self.profiles.binding(profile_id)
            if profile_id
            else ("默认 API", self.profiles.settings.llm_model or "未命名模型")
        )
        claim = current_workflow_claim.get()
        call_id = await self.profiles.usage.start(
            profile_id or "default",
            api_name,
            model,
            claim.task_id if claim else current_parent.get(),
        )
        try:
            result = await provider.generate(messages, tools=tools, tool_choice=tool_choice)
        except BaseException as error:
            from autoscholar.llm.errors import LLMResponseError, LLMUpstreamError

            outcome = (
                "not_sent"
                if isinstance(error, LocalRequestRejected)
                else "failed"
                if isinstance(error, LLMUpstreamError)
                or getattr(error, "code", "") == "llm_rate_limited"
                else "uncertain"
            )
            usage = error.usage if isinstance(error, LLMResponseError) else None
            if isinstance(error, LLMResponseError):
                self.completion.set((call_id, outcome, usage))
            else:
                await self._record_finish(call_id, outcome, usage)
            raise
        self.completion.set((call_id, "succeeded", result.usage))
        return result

    async def record_usage(self) -> None:
        completion = self.completion.get()
        self.completion.set(None)
        if completion is not None:
            await self._record_finish(*completion)

    async def _record_finish(self, call_id: str, outcome: str, usage: TokenUsage | None) -> None:
        try:
            async with asyncio.timeout(2):
                await self.profiles.usage.finish(call_id, outcome, usage)
        except Exception:
            # Preserve the actual response and workflow budget. The durable pending row
            # remains visibly unconfirmed; never resend a model request to repair metrics.
            structlog.get_logger(__name__).warning("model_usage_save_failed")

    async def close(self) -> None:
        await self.profiles.close()
        await self.default.close()
