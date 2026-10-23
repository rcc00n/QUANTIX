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


async def test_tiny_rounding_stays_cash_without_free_crypto():
    ex, asset = uuid4(), uuid4()
    key = str(ex), str(asset)
    allocations = await allocate_weighted(
        D("0.000000000000000001"),
        {key: D(1)},
        {key: D("99999999999999999999")},
        {},
        {ex: SimpleNamespace(slug="synthetic")},
        {asset: SimpleNamespace(canonical_key="synthetic:expensive")},
        SimpleNamespace(slug="synthetic"),
    )
    assert len(allocations) == 1 and allocations[0]["asset"] == "fiat:CAD"
    assert D(allocations[0]["cad_value"]) == D("1e-18")


async def test_complete_route_policy_funds_paired_legs_instead_of_thin_cells():
    exchange = uuid4()
    assets = {
        uuid4(): SimpleNamespace(canonical_key="synthetic:" + str(i)) for i in range(40)
    }
    keys = list(assets)
    routes = []
    for index in range(20):
        required = [
            {
                "exchange_id": str(exchange),
                "exchange": "synthetic",
                "asset_id": str(asset),
                "canonical_key": assets[asset].canonical_key,
                "required_quantity": "2.5",
            }
            for asset in keys[2 * index : 2 * index + 2]
        ]
        routes.append({"route_id": str(index), "required": required, "reasons": []})

    async def marks(items):
        return {
            (x["exchange_id"], x["asset_id"]): {
                **x,
                "cad_per_unit": "100",
                "received_at": NOW.isoformat(),
                "source": "SYNTHETIC_DEPTH",
            }
            for x in items
        }

    allocations = await allocate_complete_routes(
        D(9000),
        routes,
        marks,
        {exchange: SimpleNamespace(slug="synthetic")},
        assets,
        SimpleNamespace(slug="synthetic"),
    )
    assert sum(D(x["cad_value"]) for x in allocations) == 9000
    assert classify_funded_requirements(routes, allocations) == 18
    assert all(routes[i]["funded"] for i in range(18))
    assert not routes[18]["funded"] and not routes[19]["funded"]
    # No one-sided funding: each admitted pair has both original-unit requirements.
    assert len(allocations) == 36


async def test_complete_route_missing_one_mark_preserves_all_capital_as_cash():
    ex, a, b = uuid4(), uuid4(), uuid4()
    assets = {
        a: SimpleNamespace(canonical_key="synthetic:quote"),
        b: SimpleNamespace(canonical_key="synthetic:base"),
    }
    route = {
        "route_id": "synthetic",
        "required": [
            {
                "exchange_id": str(ex),
                "exchange": "synthetic",
                "asset_id": str(asset),
                "canonical_key": assets[asset].canonical_key,
                "required_quantity": "2.5",
            }
            for asset in (a, b)
        ],
        "reasons": [],
    }

    async def missing(items):
        x = items[0]
        return {
            (x["exchange_id"], x["asset_id"]): {
                **x,
                "cad_per_unit": "100",
                "received_at": NOW.isoformat(),
            }
        }

    allocations = await allocate_complete_routes(
        D(1000),
        [route],
        missing,
        {ex: SimpleNamespace(slug="synthetic")},
        assets,
        SimpleNamespace(slug="synthetic"),
    )
    assert len(allocations) == 1 and allocations[0]["asset"] == "fiat:CAD"
    assert D(allocations[0]["cad_value"]) == 1000
    assert classify_funded_requirements([route], allocations) == 0
    assert "FULL_SIZE_CAD_MARK_UNAVAILABLE" in route["reasons"]


async def test_complete_routes_share_maximum_units_not_free_or_concurrent_inventory():
    buy, sell, cad, coin = uuid4(), uuid4(), uuid4(), uuid4()
    exchanges = {
        buy: SimpleNamespace(slug="synthetic-buy"),
        sell: SimpleNamespace(slug="synthetic-sell"),
    }
    assets = {
        cad: SimpleNamespace(canonical_key="fiat:CAD"),
        coin: SimpleNamespace(canonical_key="synthetic:BTC"),
    }
    routes = []
    for quantity in ("1", "2"):
        routes.append(
            {
                "route_id": quantity,
                "reasons": [],
                "required": [
                    {
                        "exchange_id": str(buy),
                        "exchange": "synthetic-buy",
                        "asset_id": str(cad),
                        "canonical_key": "fiat:CAD",
                        "required_quantity": "250",
                    },
                    {
                        "exchange_id": str(sell),
                        "exchange": "synthetic-sell",
                        "asset_id": str(coin),
                        "canonical_key": "synthetic:BTC",
                        "required_quantity": quantity,
                    },
                ],
            }
        )

    async def marks(items):
        return {
            (x["exchange_id"], x["asset_id"]): {
                **x,
                "cad_per_unit": "100",
                "received_at": NOW.isoformat(),
            }
            for x in items
        }

    allocations = await allocate_complete_routes(
        D(500), routes, marks, exchanges, assets, exchanges[buy]
    )
    assert sum(D(x["cad_value"]) for x in allocations) == 500
    assert (
        next(D(x["quantity"]) for x in allocations if x["asset"] == "synthetic:BTC")
        == 2
    )
    assert classify_funded_requirements(routes, allocations) == 2


def test_persistent_preferences_validate_night_thresholds_and_decimal_inputs():
    with pytest.raises(ValidationError):
        TerminalSettings.model_validate(
            {"notifications": {"minimum_profit_cad": "100"}}
        )
    with pytest.raises(ValidationError):
        TerminalSettings.model_validate({"market": {"minimum_liquidity": 0.5}})
    with pytest.raises(ValidationError):
        TerminalSettings.model_validate({"market": {"maximum_age_seconds": 61}})
    with pytest.raises(ValidationError):
        TerminalSettings.model_validate({"notifications": {"timezone": "UTC"}})
    value = TerminalSettings.model_validate(
        {
            "notifications": {
                "telegram_enabled": True,
                "push_requested": True,
                "voice_requested": True,
            }
        }
    )
    assert value.notifications.telegram_enabled and value.notifications.voice_requested


def test_unstorable_or_zero_quantities_remain_cash():
    assert quantity_for_budget(D("10000"), D("1e-18")) is None
    assert quantity_for_budget(D("1e-18"), D("1e19")) is None
    assert quantity_for_budget(D("1000"), D("0")) is None


def test_default_nonzero_slippage_budget_is_storable_and_depth_sized():
    from arbiq.opportunities.analysis import dump_input
    from arbiq.paper_trading.funding import required_quantities
    from arbiq.settings.strategy import presets

    buy = execution_fixtures.book(uuid4(), "synthetic-buy", execution_fixtures.NOW)
    sell = execution_fixtures.book(
        uuid4(), "synthetic-sell", execution_fixtures.NOW, bid="110", ask="111"
    )
    evaluation = SimpleNamespace(
        evidence={
            "calculation_inputs": {
                "buy": dump_input(execution_fixtures.inputs(buy, "synthetic-buy")),
                "sell": dump_input(execution_fixtures.inputs(sell, "synthetic-sell")),
            }
        },
        evaluated_at=execution_fixtures.NOW,
    )
    best, quote, base = required_quantities(
        evaluation, presets()["Aggressive Research"]
    )
    assert best.buy_quantity > 0 and quote <= 250 and base > 0
    assert quote.as_tuple().exponent >= -18




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














