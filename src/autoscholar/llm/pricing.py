"""Optional user-supplied rates. Never infer prices or reprice historical calls."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from autoscholar.agent.database_models import Base
from autoscholar.llm.models import TokenUsage

Rate = Annotated[Decimal, Field(ge=0, le=1000000, max_digits=13, decimal_places=6)]


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["CNY", "USD"]
    input_per_million: Rate
    output_per_million: Rate
    cached_input_per_million: Rate | None = None


class ModelPricingRow(Base):
    __tablename__ = "model_prices"

    profile_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # No secrets; immutable with its model connection. Keep the model table intact.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


def estimate(pricing: ModelPricing, usage: TokenUsage) -> Decimal | None:
    if min(usage.input_tokens, usage.output_tokens, usage.total_tokens) < 0:
        return None
    if usage.input_tokens + usage.output_tokens != usage.total_tokens:
        return None
    hit_rate = pricing.cached_input_per_million
    inputs = Decimal(usage.input_tokens) * pricing.input_per_million
    if hit_rate is not None and hit_rate != pricing.input_per_million:
        hit, miss = usage.cached_input_tokens, usage.uncached_input_tokens
        if hit is None or miss is None or min(hit, miss) < 0 or hit + miss != usage.input_tokens:
            return None
        inputs = Decimal(hit) * hit_rate + Decimal(miss) * pricing.input_per_million
    # Reasoning tokens are already included in output; never add them again.
    return (
        (inputs + Decimal(usage.output_tokens) * pricing.output_per_million) / Decimal(1000000)
    ).quantize(Decimal("0.0000000001"))
