"""Per-call usage ledger. No credentials, prompts, endpoints or model output are stored."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
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


def aggregates() -> list[ColumnElement[Any]]:
    row = ModelUsageRow
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
    ]


class UsageStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def start(self, api_id: str, api_name: str, model: str, task_id: str | None) -> str:
        call_id = str(uuid4())
        try:
            async with self.sessions() as session, session.begin():
                session.add(
                    ModelUsageRow(
                        id=call_id,
                        api_id=api_id,
                        api_name=api_name,
                        model=model,
                        task_id=task_id,
                        outcome="pending",
                        usage_known=False,
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
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
            )
        async with self.sessions() as session, session.begin():
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
        zero = {key: 0 for key in UsageCounts.model_fields}
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
        )
