"""Offline immutable price snapshots, currencies, cache coverage and migration checks."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from autoscholar.llm.models import TokenUsage
from autoscholar.llm.pricing import ModelPricing, ModelPricingRow, estimate
from autoscholar.llm.usage import ModelUsageRow
from tests import test_model_profiles
from tests.test_model_profiles import connection
from tests.test_model_usage import store

client = test_model_profiles.client
RATES = {
    "currency": "CNY",
    "input_per_million": "2",
    "output_per_million": "8",
    "cached_input_per_million": "0.5",
}


@pytest.mark.parametrize(
    "cached,expected",
    [(None, None), (0, "0.0036"), (500, "0.00285"), (1000, "0.0021"), (1001, None)],
)
def test_estimates_need_valid_cache_splits_and_never_double_count_reasoning(
    cached: int | None,
    expected: str | None,
) -> None:
    usage = TokenUsage(
        1000,
        200,
        1200,
        cached_input_tokens=cached,
        uncached_input_tokens=1000 - cached if cached is not None else None,
        reasoning_tokens=100,
    )
    assert estimate(ModelPricing.model_validate(RATES), usage) == (
        Decimal(expected) if expected is not None else None
    )


def test_uniform_rates_can_estimate_without_cache_metadata_and_validate_totals() -> None:
    rates = ModelPricing.model_validate(
        dict(currency="USD", input_per_million="2", output_per_million="8")
    )
    assert estimate(rates, TokenUsage(1000, 200, 1200)) == Decimal("0.0036")
    assert estimate(rates, TokenUsage(1000, 200, 1199)) is None


async def test_rates_are_optional_immutable_and_idempotent_without_calls(
    client: httpx.AsyncClient,
) -> None:
    payload = connection()
    created = await client.post("/workbench/models", json=payload | {"pricing": RATES})
    assert created.status_code == 201
    assert created.json()["pricing"] == RATES
    assert "api_key" not in created.json()
    assert (
        await client.post("/workbench/models", json=payload | {"pricing": RATES})
    ).status_code == 201
    assert (await client.post("/workbench/models", json=payload)).status_code == 409
    assert (await client.get("/workbench/models")).json()["items"][0]["pricing"] == RATES
    disabled = await client.post(f"/workbench/models/{payload['id']}/disable")
    assert disabled.status_code == 200 and disabled.json()["pricing"] == RATES


async def test_ledger_freezes_rates_and_separates_currencies_and_unknown_costs(
    client: httpx.AsyncClient,
) -> None:
    ledger = store(client)
    async with ledger.sessions() as session:
        session.add(ModelPricingRow(profile_id="cny", payload=RATES))
        session.add(ModelPricingRow(profile_id="usd", payload=RATES | {"currency": "USD"}))
        await session.commit()
    first = await ledger.start("cny", "CNY fixture", "shared-model", "root", stage="refining")
    async with ledger.sessions() as session:
        price = await session.get(ModelPricingRow, "cny")
        assert price
        # Deliberately mutate the underlying fixture to prove calls use their snapshot.
        price.payload = RATES | {"input_per_million": "100"}
        await session.commit()
    usage = TokenUsage(1000, 200, 1200, cached_input_tokens=500, uncached_input_tokens=500)
    await ledger.finish(first, "succeeded", usage)
    second = await ledger.start("usd", "USD fixture", "shared-model", "root")
    await ledger.finish(second, "succeeded", usage)
    third = await ledger.start("cny", "CNY fixture", "shared-model", "root")
    await ledger.finish(third, "succeeded", TokenUsage(1000, 200, 1200))
    fourth = await ledger.start("unpriced", "No rates", "other-model", "other")
    await ledger.finish(fourth, "succeeded", usage)
    summary = await ledger.summary(days=0, group_by="model")
    assert summary.totals.estimated_cny == pytest.approx(0.00285)
    assert summary.totals.estimated_usd == pytest.approx(0.00285)
    assert summary.totals.cost_known_calls == summary.totals.unknown_cost_calls == 2
    assert summary.totals.total_tokens == 4800
    filtered = await ledger.summary(days=0, api_id="cny")
    assert filtered.totals.estimated_usd is None and filtered.totals.calls == 2
    async with ledger.sessions() as session:
        record = await session.scalar(select(ModelUsageRow).where(ModelUsageRow.id == first))
        assert record and record.pricing_snapshot == RATES
        assert record.estimated_cost == Decimal("0.00285")


def test_price_migration_preserves_history_and_round_trips(tmp_path: Path) -> None:
    database = tmp_path / "prices.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", "sqlite+aiosqlite:///" + database.as_posix())
    with sqlite3.connect(database) as sql:
        sql.execute("CREATE TABLE model_profiles (id VARCHAR(36) PRIMARY KEY)")
        sql.execute("INSERT INTO model_profiles VALUES ('old-profile')")
    command.stamp(config, "20261106_0016")
    command.upgrade(config, "20261107_0018")
    with sqlite3.connect(database) as sql:
        sql.execute(
            "INSERT INTO model_usage (id,api_id,api_name,model,outcome,usage_known,"
            "input_tokens,output_tokens,total_tokens) VALUES "
            "('old','old-profile','API','model','failed',1,100,20,120)"
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database) as sql:
        assert sql.execute(
            "SELECT outcome,total_tokens,pricing_snapshot,estimated_cost,currency FROM model_usage"
        ).fetchone() == ("failed", 120, None, None, None)
        assert sql.execute("SELECT * FROM model_profiles").fetchall() == [("old-profile",)]
    command.downgrade(config, "20261107_0018")
    with sqlite3.connect(database) as sql:
        assert sql.execute("SELECT total_tokens FROM model_usage").fetchone() == (120,)
    command.upgrade(config, "head")
