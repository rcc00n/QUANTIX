"""SYNTHETIC funding, configuration and retained replay. Never live deposits/fills."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_paper_api as paper_fixtures
import test_paper_execution as execution_fixtures
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageRoute,
    Exchange,
    PaperFill,
)
from arbiq.paper_trading.funding import (
    allocate_complete_routes,
    allocate_weighted,
    classify_funded_requirements,
    missing_inventory,
    quantity_for_budget,
)
from arbiq.portfolio.accounting import reconcile
from arbiq.portfolio.contracts import stored
from arbiq.settings.terminal import TerminalSettings
from pydantic import ValidationError
from sqlalchemy import func, select, update

private_api = paper_fixtures.private_api
environment = execution_fixtures.environment

D = Decimal
NOW = datetime(2026, 11, 22, 6, tzinfo=UTC)


def test_exact_insufficient_inventory_is_in_own_asset_units():
    required = [
        {
            "exchange_id": "synthetic-A",
            "asset_id": "synthetic-USDT",
            "required_quantity": "100.123456789012345678",
        },
        {
            "exchange_id": "synthetic-B",
            "asset_id": "synthetic-BTC",
            "required_quantity": "0.012345678901234567",
        },
    ]
    value = missing_inventory(
        required,
        {
            ("synthetic-A", "synthetic-USDT"): D("99"),
            ("synthetic-B", "synthetic-BTC"): D("0"),
        },
    )
    assert D(value[0]["missing_quantity"]) == D("1.123456789012345678")
    assert D(value[1]["missing_quantity"]) == D("0.012345678901234567")


@pytest.mark.parametrize(
    "missing,price,expected_assets",
    [(False, "100", 1), (True, "100", 1), (False, "101", 1), (False, "0", 1)],
)
async def test_capital_allocation_preserves_every_cent_and_depth(
    missing, price, expected_assets
):
    ex, asset = uuid4(), uuid4()
    exchanges = {ex: SimpleNamespace(slug="synthetic-venue")}
    assets = {asset: SimpleNamespace(canonical_key="synthetic:BTC")}
    key = str(ex), str(asset)
    marks = (
        {}
        if missing
        else {
            key: {
                "cad_per_unit": price,
                "received_at": NOW.isoformat(),
                "quantity": "10",
                "verified": False,
            }
        }
    )
    allocations = await allocate_weighted(
        D("1000"), {key: D(1)}, {key: D(100)}, marks, exchanges, assets, exchanges[ex]
    )
    assert len(allocations) == expected_assets
    assert (allocations[0]["asset"] == "synthetic:BTC") == (
        not missing and price == "100"
    )
    assert sum(D(x["cad_value"]) for x in allocations) == 1000
    for item in allocations:
        assert D(item["quantity"]) > 0 and D(item["cad_value"]) > 0
        assert stored(D(item["quantity"]) * D(item["cad_rate"])) == D(item["cad_value"])


















@pytest.fixture
async def funding_api(private_api, monkeypatch):
    async def clock(request):
        return NOW

    for path in (
        "arbiq.paper_trading.funding.now_checked",
        "arbiq.settings.api.now_checked",
        "arbiq.paper_trading.replay_api.now_checked",
    ):
        monkeypatch.setattr(path, clock)
    async with private_api[1]() as db:
        await db.execute(
            update(Exchange)
            .where(Exchange.slug.in_(["kraken", "ndax"]))
            .values(enabled=True)
        )
        await db.commit()
    return private_api














