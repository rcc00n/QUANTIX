"""SYNTHETIC read projections preserve every financial/filter/display decision.

SQL checks run only in the dedicated _test database and perform SELECTs only.
No financial fixture is inserted into an application database.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from arbiq.opportunities import api
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from test_opportunity_api import (
    NOW,
    synthetic_health,
    synthetic_payload,
    synthetic_route,
)

D = Decimal


def synthetic_evidenced_payload():
    expiry = (NOW + timedelta(seconds=5)).isoformat()
    return synthetic_payload(
        supporting_source_at=None,
        costs={
            "capital_requirements": {"SYNTHETIC_BUY": {"USD": "100"}},
            "fees_cad": "1.2",
            "fx_cost_cad": "0.5",
            "slippage_reserve_cad": "0.1",
            "rebalancing_cost_cad": "0.2",
        },
        maximum_executable={"acquisition_cost_cad": "500.000000000000000001"},
        budget_results=[{"SYNTHETIC_full_budget_evidence": "preserved in detail"}],
        transfer_scenario={"SYNTHETIC_transfer_evidence": "preserved in detail"},
        optimizer={"SYNTHETIC_optimality": "preserved in detail"},
        evidence={
            "buy_quality": "DEGRADED",
            "sell_quality": "SUSPICIOUS",
            "buy_book": {"exchange_timestamp": NOW.isoformat()},
            "sell_book": {"exchange_timestamp": NOW.isoformat()},
            "fx_books": {"SYNTHETIC": ["unneeded full FX depth"] * 25},
            "calculation_inputs": {
                "evaluated_at": NOW.isoformat(),
                "buy": {
                    "quote_currency": "USD",
                    "asks": [{"SYNTHETIC": "depth"}],
                    "bids": [{"SYNTHETIC": "depth"}],
                    "fx": {
                        "fresh_until": expiry,
                        "to_cad": {"legs": ["SYNTHETIC"]},
                        "from_cad": {"legs": ["SYNTHETIC"]},
                    },
                },
                "sell": {
                    "quote_currency": "CAD",
                    "asks": [{"SYNTHETIC": "depth"}],
                    "bids": [{"SYNTHETIC": "depth"}],
                    "fx": None,
                },
            },
        },
    )


def displayed(payload, health):
    result = api.project(synthetic_route(payload), D(1000), health, 30)
    # Keep route identity fixed across equivalent projections in these comparisons.
    result.pop("id")
    return {k: v for k, v in result.items() if k not in api.LIST_DETAIL_FIELDS}


def test_list_query_selects_only_needed_scalar_columns_and_safe_json_case():
    query = api.list_route_query(D(1000))
    assert [column.key for column in query.selected_columns] == [
        "id",
        "status",
        "detected_at",
        "updated_at",
        "expires_at",
        "payload",
    ]
    statement = str(query.compile(dialect=postgresql.dialect()))
    assert "CASE WHEN" in statement and "#-" in statement
    assert "ELSE arbitrage_routes.payload" in statement
    full = str(
        api.list_route_query(D(250), full=True).compile(dialect=postgresql.dialect())
    )
    assert "#-" not in full
    assert "arbitrage_routes.payload" in full


@pytest.mark.parametrize(
    "seconds,trusted,host,healthy",
    [
        (0, True, True, True),
        (6, True, True, True),
        (31, True, True, True),
        (-3, True, True, True),
        (0, False, True, True),
        (0, True, False, True),
        (0, True, True, False),
    ],
)
async def test_sql_projection_preserves_freshness_quality_financial_filters_and_exact_sort(
    database_url,
    seconds,
    trusted,
    host,
    healthy,
):
    original = synthetic_evidenced_payload()
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            compact = await db.scalar(
                select(
                    api.list_payload_expression(
                        D(1000),
                        payload_column=bindparam("synthetic_payload", type_=JSONB),
                    )
                ),
                {"synthetic_payload": original},
            )
        # Heavy display-unneeded trees are pruned only from the SQL read result.
        assert "fx_books" not in compact["evidence"]
        assert "asks" not in compact["evidence"]["calculation_inputs"]["buy"]
        assert "to_cad" not in compact["evidence"]["calculation_inputs"]["buy"]["fx"]
        assert "budget_results" not in compact
        assert original["budget_results"]
        assert original["evidence"]["fx_books"]
        health = synthetic_health(
            NOW + timedelta(seconds=seconds), trusted, host, healthy
        )
        before, after = displayed(original, health), displayed(compact, health)
        assert before == after
        assert after["data_confidence"] == "SUSPICIOUS"
        for filters in (
            {"search": "SYNTH"},
            {"confidence": "SUSPICIOUS"},
            {"min_profit_cad": D(10)},
            {"min_return_pct": D(10)},
            {"min_quantity": D(1)},
            {"min_liquidity_cad": D("500.000000000000000001")},
            {"max_acquisition_cad": D(100)},
        ):
            assert api.meets_filters(before, **filters) == api.meets_filters(
                after, **filters
            )
        assert api.financial_sort(
            {**before, "id": "SYNTHETIC"}, "net_profit_cad"
        ) == api.financial_sort(
            {**after, "id": "SYNTHETIC"},
            "net_profit_cad",
        )
    finally:
        await engine.dispose()




