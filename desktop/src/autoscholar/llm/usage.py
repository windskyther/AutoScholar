"""Per-call usage ledger. No credentials, prompts, endpoints or model output are stored."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Numeric,
    String,
    case,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from autoscholar.agent.database_models import Base
from autoscholar.core.errors import AppError
from autoscholar.core.rejections import LocalRequestRejected
from autoscholar.llm.models import TokenUsage
from autoscholar.llm.pricing import ModelPricing, ModelPricingRow, estimate

LOCAL_ZONE = timezone(timedelta(hours=8))


class ModelUsageRow(Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        Index("ix_model_usage_created", "created_at"),
        Index("ix_model_usage_api_created", "api_id", "created_at"),
        Index("ix_model_usage_model_created", "model", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Deliberately no task FK: removing a task must not erase its recorded API usage.
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_id: Mapped[str] = mapped_column(String(64))
    api_name: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(16), default="pending")
    usage_known: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uncached_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stage: Mapped[str] = mapped_column(String(24), default="other", server_default="other")
    pricing_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageUnavailable(AppError, LocalRequestRejected):
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            code="model_usage_unavailable",
            message="Usage recording is unavailable; no model request was sent",
        )


class UsageCounts(BaseModel):
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    unknown_usage_calls: int = Field(ge=0)
    failed_calls: int = Field(ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    cache_known_calls: int = Field(default=0, ge=0)
    unknown_cache_calls: int = Field(default=0, ge=0)
    cache_known_input_tokens: int = Field(default=0, ge=0)
    estimated_cny: float | None = Field(default=None, ge=0)
    estimated_usd: float | None = Field(default=None, ge=0)
    cost_known_calls: int = Field(default=0, ge=0)
    unknown_cost_calls: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cache_hit_rate(self) -> float | None:
        return (
            self.cached_input_tokens / self.cache_known_input_tokens
            if self.cached_input_tokens is not None and self.cache_known_input_tokens
            else None
        )


class UsageGroup(UsageCounts):
    id: str
    label: str


class UsageDay(UsageCounts):
    date: str


class UsageAPI(BaseModel):
    id: str
    name: str


class UsageOptions(BaseModel):
    apis: list[UsageAPI]
    models: list[str]


class UsageSummary(BaseModel):
    group_by: Literal["api", "model"]
    days: Literal[0, 7, 30, 90]
    utc_offset_minutes: int = 480
    recorded_since: datetime | None
    trend_from: str
    totals: UsageCounts
    groups: list[UsageGroup]
    daily: list[UsageDay]
    options: UsageOptions
    has_more_groups: bool = False
    stages: list[UsageGroup] = Field(default_factory=list)


def aggregates() -> list[ColumnElement[Any]]:
    row = ModelUsageRow
    known = row.cached_input_tokens.is_not(None) & row.uncached_input_tokens.is_not(None)
    return [
        func.count().label("calls"),
        func.coalesce(func.sum(row.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(row.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(row.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(case((row.usage_known.is_(False), 1), else_=0)), 0).label(
            "unknown_usage_calls"
        ),
        func.coalesce(func.sum(case((row.outcome == "failed", 1), else_=0)), 0).label(
            "failed_calls"
        ),
        func.sum(row.cached_input_tokens).label("cached_input_tokens"),
        func.sum(row.uncached_input_tokens).label("uncached_input_tokens"),
        func.coalesce(func.sum(case((known, 1), else_=0)), 0).label("cache_known_calls"),
        func.coalesce(func.sum(case((~known, 1), else_=0)), 0).label("unknown_cache_calls"),
        func.coalesce(func.sum(case((known, row.input_tokens), else_=0)), 0).label(
            "cache_known_input_tokens"
        ),
        func.sum(case((row.currency == "CNY", row.estimated_cost))).label("estimated_cny"),
        func.sum(case((row.currency == "USD", row.estimated_cost))).label("estimated_usd"),
        func.coalesce(func.sum(case((row.estimated_cost.is_not(None), 1), else_=0)), 0).label(
            "cost_known_calls"
        ),
        func.coalesce(func.sum(case((row.estimated_cost.is_(None), 1), else_=0)), 0).label(
            "unknown_cost_calls"
        ),
    ]


class UsageStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def start(
        self, api_id: str, api_name: str, model: str, task_id: str | None, *, stage: str = "other"
    ) -> str:
        call_id = str(uuid4())
        try:
            async with self.sessions() as session, session.begin():
                price_row = await session.get(ModelPricingRow, api_id)
                price = ModelPricing.model_validate(price_row.payload) if price_row else None
                session.add(
                    ModelUsageRow(
                        id=call_id,
                        api_id=api_id,
                        api_name=api_name,
                        model=model,
                        task_id=task_id,
                        stage=stage,
                        outcome="pending",
                        usage_known=False,
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                        pricing_snapshot=price.model_dump(mode="json") if price else None,
                        currency=price.currency if price else None,
                    )
                )
        except Exception as exc:
            raise UsageUnavailable() from exc
        return call_id

    async def finish(self, call_id: str, outcome: str, usage: TokenUsage | None) -> None:
        values: dict[str, object] = {"outcome": outcome, "usage_known": usage is not None}
        if usage is not None:
            values.update(
                input_tokens=max(0, usage.input_tokens),
                output_tokens=max(0, usage.output_tokens),
                total_tokens=max(0, usage.total_tokens),
                cached_input_tokens=usage.cached_input_tokens,
                uncached_input_tokens=usage.uncached_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            )
        async with self.sessions() as session, session.begin():
            if usage is not None:
                row = await session.get(ModelUsageRow, call_id)
                if row is not None and row.pricing_snapshot is not None:
                    values["estimated_cost"] = estimate(
                        ModelPricing.model_validate(row.pricing_snapshot), usage
                    )
            await session.execute(
                update(ModelUsageRow).where(ModelUsageRow.id == call_id).values(**values)
            )

    async def summary(
        self,
        *,
        group_by: Literal["api", "model"] = "api",
        days: Literal[0, 7, 30, 90] = 30,
        api_id: str | None = None,
        model: str | None = None,
        now: datetime | None = None,
    ) -> UsageSummary:
        now = now or datetime.now(UTC)
        today = now.astimezone(LOCAL_ZONE).date()
        first_day = today - timedelta(days=(days or 90) - 1)
        trend_start = datetime.combine(first_day, datetime.min.time(), LOCAL_ZONE).astimezone(UTC)
        row = ModelUsageRow
        filters = [row.outcome != "not_sent", row.created_at <= now]
        if days:
            filters.append(row.created_at >= trend_start)
        if api_id:
            filters.append(row.api_id == api_id)
        if model:
            filters.append(row.model == model)
        async with self.sessions() as session:
            if session.get_bind().dialect.name == "postgresql":
                # Keep totals, groups and daily series on one read snapshot while
                # model responses update the ledger in other transactions.
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            totals = (
                (await session.execute(select(*aggregates()).select_from(row).where(*filters)))
                .mappings()
                .one()
            )
            identity = row.api_id if group_by == "api" else row.model
            label = func.max(row.api_name) if group_by == "api" else row.model
            groups = (
                (
                    await session.execute(
                        select(identity.label("id"), label.label("label"), *aggregates())
                        .where(*filters)
                        .group_by(identity)
                        .order_by(func.sum(row.total_tokens).desc(), func.count().desc(), identity)
                        .limit(501)
                    )
                )
                .mappings()
                .all()
            )
            bucket = (
                func.strftime("%Y-%m-%d", row.created_at, "+8 hours")
                if session.get_bind().dialect.name == "sqlite"
                else func.to_char(func.timezone("Asia/Taipei", row.created_at), "YYYY-MM-DD")
            )
            daily = (
                (
                    await session.execute(
                        select(bucket.label("date"), *aggregates())
                        .where(*filters, row.created_at >= trend_start)
                        .group_by(bucket)
                        .order_by(bucket)
                    )
                )
                .mappings()
                .all()
            )
            since = await session.scalar(
                select(func.min(row.created_at)).where(row.outcome != "not_sent")
            )
            stages = (
                (
                    await session.execute(
                        select(row.stage.label("id"), row.stage.label("label"), *aggregates())
                        .where(*filters)
                        .group_by(row.stage)
                        .order_by(row.stage)
                    )
                )
                .mappings()
                .all()
            )
            apis = (
                await session.execute(
                    select(row.api_id, func.max(row.api_name).label("name"))
                    .where(row.outcome != "not_sent")
                    .group_by(row.api_id)
                    .order_by(row.api_id)
                    .limit(500)
                )
            ).all()
            models = list(
                await session.scalars(
                    select(row.model)
                    .where(row.outcome != "not_sent")
                    .distinct()
                    .order_by(row.model)
                    .limit(500)
                )
            )
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        zero = {
            key: None
            if key
            in {"cached_input_tokens", "uncached_input_tokens", "estimated_cny", "estimated_usd"}
            else 0
            for key in UsageCounts.model_fields
        }
        by_day = {item["date"]: dict(item) for item in daily}
        return UsageSummary(
            group_by=group_by,
            days=days,
            recorded_since=since,
            trend_from=first_day.isoformat(),
            totals=UsageCounts.model_validate(dict(totals)),
            groups=[UsageGroup.model_validate(dict(item)) for item in groups[:500]],
            daily=[
                UsageDay.model_validate(
                    by_day.get(
                        (first_day + timedelta(days=index)).isoformat(),
                        {"date": (first_day + timedelta(days=index)).isoformat(), **zero},
                    )
                )
                for index in range(days or 90)
            ],
            options=UsageOptions(
                apis=[UsageAPI(id=item.api_id, name=item.name) for item in apis], models=models
            ),
            has_more_groups=len(groups) > 500,
            stages=[UsageGroup.model_validate(dict(item)) for item in stages],
        )
