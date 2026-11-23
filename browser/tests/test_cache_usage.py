import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config

from autoscholar.core.budget import Budget, BudgetLimits, current_budget
from autoscholar.llm.models import TokenUsage
from autoscholar.llm.profiles import RoutedLLM
from autoscholar.llm.stages import llm_stage
from autoscholar.llm.usage_parser import parse_usage
from tests import test_model_profiles
from tests.test_model_usage import ManagedMeter, Provider, store

client = test_model_profiles.client


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({}, (None, None)),
        ({"prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 30}, (70, 30)),
        ({"prompt_tokens_details": {"cached_tokens": 70}}, (70, 30)),
        ({"prompt_cache_hit_tokens": 0}, (0, 100)),
        ({"prompt_cache_miss_tokens": 30}, (70, 30)),
        ({"prompt_cache_hit_tokens": 120}, (None, None)),
        ({"prompt_cache_hit_tokens": -1}, (None, None)),
        ({"prompt_cache_hit_tokens": True}, (None, None)),
        ({"prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 40}, (None, None)),
        (
            {"prompt_cache_hit_tokens": 70, "prompt_tokens_details": {"cached_tokens": 60}},
            (None, None),
        ),
    ],
)
def test_cache_reports_are_validated_without_changing_total_usage(
    extra: dict[str, object], expected: tuple[int | None, int | None]
) -> None:
    raw = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120, **extra)
    usage = parse_usage(raw)
    assert (usage.cached_input_tokens, usage.uncached_input_tokens) == expected
    assert usage.total_tokens == 120


async def test_cache_ledger_keeps_unknowns_and_actual_budget(client: httpx.AsyncClient) -> None:
    profiles = client.fixture_app.state.model_profiles  # type: ignore[attr-defined]
    budget = Budget(BudgetLimits())
    token = current_budget.set(budget)
    try:
        with llm_stage("refining"):
            await ManagedMeter(
                RoutedLLM(Provider(usage=TokenUsage(100, 20, 120, 70, 30)), profiles)
            ).generate([])
        await ManagedMeter(RoutedLLM(Provider(usage=TokenUsage(50, 10, 60)), profiles)).generate([])
    finally:
        current_budget.reset(token)
    summary = await store(client).summary()
    assert summary.totals.total_tokens == budget.used["total_tokens"] == 180
    assert summary.totals.cached_input_tokens == 70
    assert summary.totals.uncached_input_tokens == 30
    assert summary.totals.unknown_cache_calls == 1
    assert summary.totals.cache_hit_rate == 0.7
    assert {item.id: item.calls for item in summary.stages} == {"refining": 1, "other": 1}
    payload = (await client.get("/workbench/usage")).json()
    assert payload["totals"]["cache_hit_rate"] == 0.7


async def test_all_unknown_is_not_reported_as_zero_cache(client: httpx.AsyncClient) -> None:
    ledger = store(client)
    identifier = await ledger.start("api", "Public fixture", "model", None)
    await ledger.finish(identifier, "succeeded", TokenUsage(100, 20, 120))
    summary = await ledger.summary()
    assert summary.totals.cached_input_tokens is None
    assert summary.totals.uncached_input_tokens is None
    assert summary.totals.cache_hit_rate is None
    assert summary.totals.unknown_cache_calls == 1


def test_cache_migration_preserves_historical_unknowns_and_outcomes(tmp_path: Path) -> None:
    database = tmp_path / "cache.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + database.as_posix())
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE model_profiles (id VARCHAR(36) PRIMARY KEY)")
    command.stamp(config, "20261106_0016")
    command.upgrade(config, "20261107_0017")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO model_usage (id,api_id,api_name,model,outcome,usage_known,"
            "input_tokens,output_tokens,total_tokens) VALUES ('old','api','API','model',"
            "'failed',1,100,20,120)"
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT outcome,input_tokens,output_tokens,total_tokens,cached_input_tokens,"
            "uncached_input_tokens,stage FROM model_usage WHERE id='old'"
        ).fetchone() == ("failed", 100, 20, 120, None, None, "other")
    command.downgrade(config, "20261107_0017")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT total_tokens FROM model_usage").fetchone() == (120,)
