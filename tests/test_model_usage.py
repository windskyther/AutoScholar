import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.llm.errors import LLMRequestRejected, LLMResponseError
from autoscholar.llm.models import (
    ConversationMessage,
    LLMResult,
    TokenUsage,
    ToolChoice,
    ToolDefinition,
)
from autoscholar.llm.profiles import RoutedLLM
from autoscholar.llm.usage import ModelUsageRow, UsageStore, UsageUnavailable
from tests import test_model_profiles
from tests.test_model_profiles import AUTH

client = test_model_profiles.client
DEFAULT_USAGE = TokenUsage(3, 4, 7)


class Provider:
    configured = True

    def __init__(
        self, error: Exception | None = None, usage: TokenUsage | None = DEFAULT_USAGE
    ) -> None:
        self.calls = 0
        self.error, self.usage = error, usage

    async def generate(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "none",
    ) -> LLMResult:
        self.calls += 1
        if self.error:
            raise self.error
        return LLMResult(text="public fixture", model="fixture", usage=self.usage)

    async def close(self) -> None:
        pass


def store(client: httpx.AsyncClient) -> UsageStore:
    return client.fixture_app.state.model_profiles.usage  # type: ignore[attr-defined,no-any-return]


async def test_aggregation_filters_unknowns_timezone_and_no_parent_double_count(
    client: httpx.AsyncClient,
) -> None:
    ledger = store(client)
    now = datetime(2026, 11, 7, 12, tzinfo=UTC)
    async with ledger.sessions() as session:
        for index, (api, model, tokens, when, known, outcome) in enumerate(
            [
                ("a", "shared", 10, datetime(2026, 11, 6, 15, 59, tzinfo=UTC), True, "succeeded"),
                ("b", "shared", 20, datetime(2026, 11, 6, 16, tzinfo=UTC), True, "failed"),
                ("a", "other", 30, now, True, "succeeded"),
                ("a", "other", 0, now, False, "uncertain"),
                ("b", "other", 0, now, False, "pending"),
                ("a", "other", 0, now, False, "not_sent"),
                ("old", "shared", 100, now - timedelta(days=120), True, "succeeded"),
            ]
        ):
            session.add(
                ModelUsageRow(
                    id=str(index),
                    task_id="same-root",
                    api_id=api,
                    api_name=api.upper(),
                    model=model,
                    outcome=outcome,
                    usage_known=known,
                    input_tokens=tokens,
                    output_tokens=0,
                    total_tokens=tokens,
                    created_at=when,
                )
            )
        await session.commit()
    by_api = await ledger.summary(days=7, now=now)
    by_model = await ledger.summary(days=7, group_by="model", now=now)
    assert by_api.totals == by_model.totals
    assert by_api.totals.calls == 5 and by_api.totals.total_tokens == 60
    assert by_api.totals.unknown_usage_calls == 2 and by_api.totals.failed_calls == 1
    assert sum(item.total_tokens for item in by_api.groups) == 60
    assert {item.label: item.total_tokens for item in by_model.groups} == {
        "shared": 30,
        "other": 30,
    }
    assert by_api.daily[-2].total_tokens == 10 and by_api.daily[-1].total_tokens == 50
    filtered = await ledger.summary(days=7, api_id="a", model="other", now=now)
    assert filtered.totals.calls == 2 and filtered.totals.total_tokens == 30
    all_time = await ledger.summary(days=0, now=now)
    assert all_time.totals.total_tokens == 160
    assert sum(item.total_tokens for item in all_time.daily) == 60 and len(all_time.daily) == 90


async def test_usage_api_requires_auth_validates_queries_and_returns_empty_counts(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/workbench/usage")).status_code == 401
    response = await client.get(
        "/workbench/usage", params={"days": 7, "group_by": "model"}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["totals"]["calls"] == 0 and len(body["daily"]) == 7
    assert body["recorded_since"] is None
    assert not any(
        value in response.text
        for value in ("api_key", "encrypted_key", "public-model-management-operator")
    )
    for params in ({"days": 8}, {"group_by": "secret"}, {"days": -1}):
        assert (
            await client.get("/workbench/usage", params=params, headers=AUTH)
        ).status_code == 422


async def test_measured_protocol_failure_and_missing_usage_are_recorded(
    client: httpx.AsyncClient,
) -> None:
    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    await BudgetedLLM(RoutedLLM(Provider(), profiles)).generate([])
    error = LLMResponseError(
        code="llm_invalid_tool_call", message="fixture", usage=TokenUsage(3, 4, 7)
    )
    with pytest.raises(LLMResponseError):
        await BudgetedLLM(RoutedLLM(Provider(error), profiles)).generate([])
    await BudgetedLLM(RoutedLLM(Provider(usage=None), profiles)).generate([])
    summary = await store(client).summary()
    assert summary.totals.calls == 3 and summary.totals.total_tokens == 14
    assert summary.totals.failed_calls == 1 and summary.totals.unknown_usage_calls == 1


async def test_local_rejection_is_excluded_from_calls_and_budget(client: httpx.AsyncClient) -> None:
    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
    try:
        with pytest.raises(LLMRequestRejected):
            await BudgetedLLM(
                RoutedLLM(Provider(LLMRequestRejected(message="not sent")), profiles)
            ).generate([])
    finally:
        current_budget.reset(token)
    assert budget.used.get("model_calls", 0) == 0
    assert (await store(client).summary()).totals.calls == 0


async def test_ledger_start_failure_cannot_dispatch(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    provider = Provider()

    async def unavailable(*args: object) -> str:
        raise UsageUnavailable()

    monkeypatch.setattr(profiles.usage, "start", unavailable)
    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
    try:
        with pytest.raises(UsageUnavailable):
            await BudgetedLLM(RoutedLLM(provider, profiles)).generate([])
    finally:
        current_budget.reset(token)
    assert provider.calls == 0 and budget.used.get("model_calls", 0) == 0


async def test_interrupted_ledger_finish_preserves_known_budget_and_never_repeats_model(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    provider = Provider()
    budget = Budget(BudgetLimits())
    entered = asyncio.Event()

    async def wait(*args: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(profiles.usage, "finish", wait)

    async def invoke() -> None:
        token = current_budget.set(budget)
        try:
            await BudgetedLLM(RoutedLLM(provider, profiles)).generate([])
        finally:
            current_budget.reset(token)

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(entered.wait(), 3)
    assert budget.used["total_tokens"] == 7
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.calls == 1
    summary = await store(client).summary()
    assert summary.totals.calls == 1 and summary.totals.unknown_usage_calls == 1
    async with profiles.sessions() as session:
        assert (await session.scalars(select(ModelUsageRow.outcome))).one() == "pending"
