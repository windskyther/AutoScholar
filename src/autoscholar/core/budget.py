"""Request-local budgets shared by all autonomous child services."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from autoscholar.llm.base import LLMProvider
    from autoscholar.llm.models import (
        ConversationMessage,
        LLMResult,
        TokenUsage,
        ToolChoice,
        ToolDefinition,
    )


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: int = Field(default=20, ge=1, le=100)
    replans: int = Field(default=3, ge=0, le=10)
    model_calls: int = Field(default=60, ge=1, le=300)
    tool_calls: int = Field(default=50, ge=1, le=500)
    search_queries: int = Field(default=20, ge=1, le=100)
    code_repairs: int = Field(default=3, ge=0, le=10)
    training_runs: int = Field(default=4, ge=1, le=20)
    sandbox_runs: int = Field(default=40, ge=1, le=200)
    total_tokens: int = Field(default=120000, ge=1, le=1000000)
    wall_seconds: int = Field(default=1800, ge=1, le=7200)

    def bounded_by(self, ceiling: BudgetLimits) -> BudgetLimits:
        return BudgetLimits(
            **{key: min(value, getattr(ceiling, key)) for key, value in self.model_dump().items()}
        )


class BudgetExceeded(RuntimeError):
    code = "autonomous_budget_exceeded"

    def __init__(self, resource: str) -> None:
        self.message = f"Autonomous budget exhausted: {resource}"
        super().__init__(self.message)


@dataclass
class Budget:
    limits: BudgetLimits
    used: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def check(self) -> None:
        if time.monotonic() - self.started >= self.limits.wall_seconds:
            raise BudgetExceeded("wall_seconds")
        if self.used.get("total_tokens", 0) >= self.limits.total_tokens:
            raise BudgetExceeded("total_tokens")

    def consume(self, resource: str, amount: int = 1) -> None:
        self.check()
        current = self.used.get(resource, 0)
        if current + amount > int(getattr(self.limits, resource)):
            raise BudgetExceeded(resource)
        self.used[resource] = current + amount


current_budget: ContextVar[Budget | None] = ContextVar("autonomous_budget", default=None)
current_parent: ContextVar[str | None] = ContextVar("autonomous_parent", default=None)


def consume(resource: str, amount: int = 1) -> None:
    budget = current_budget.get()
    if budget is not None:
        budget.consume(resource, amount)


class BudgetedLLM:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    @property
    def configured(self) -> bool:
        return self.provider.configured

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        # Config imports BudgetLimits before the provider package can be initialized.
        from autoscholar.llm.errors import LLMResponseError

        consume("model_calls")
        try:
            result = await self.provider.generate(messages, tools=tools, tool_choice=tool_choice)
        except LLMResponseError as exc:
            self._account(exc.usage)
            raise
        self._account(result.usage)
        return result

    @staticmethod
    def _account(usage: TokenUsage | None) -> None:
        budget = current_budget.get()
        if budget is not None:
            if usage is None:
                # Never permit unmetered repeated calls when a provider omits usage.
                budget.used["total_tokens"] = budget.limits.total_tokens
                raise BudgetExceeded("provider_usage_missing")
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                budget.used[key] = budget.used.get(key, 0) + max(0, getattr(usage, key))
            budget.check()

    async def close(self) -> None:
        await self.provider.close()
