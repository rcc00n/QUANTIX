"""SYNTHETIC public book fixtures and isolated PostgreSQL planner checks."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_paper_execution as execution
import test_stage5_funding as stage5
from arbiq.db.models import ArbitrageEvaluation, ArbitrageRoute, Exchange, OrderBookSnapshot
from arbiq.opportunities.analysis import dump_input
from arbiq.paper_trading.funding import proposal_marks_fresh
from arbiq.paper_trading.funding_sizing import projected_costs, read_pair, size_at_budget
from arbiq.portfolio.accounting import reconcile
from arbiq.settings.strategy import presets
from sqlalchemy import select

D = Decimal
AT = datetime(2026, 12, 12, 12, tzinfo=UTC)
funding_api = stage5.funding_api
private_api = stage5.private_api
environment = stage5.environment


def test_receipt_only_valuation_is_considered_without_initializing_portfolio():
    payload = {
        "allocations": [
            {
                "asset": "native:SYNTHETIC",
                "evidence": {"mark": {"observed_at": None, "received_at": AT.isoformat()}},
            }
        ]
    }
    assert proposal_marks_fresh(payload, AT + timedelta(seconds=1)) is False


def pinned(buy=None, sell=None, when=AT):
    buy = buy or execution.book(uuid4(), "synthetic-buy", when)
    sell = sell or execution.book(uuid4(), "synthetic-sell", when, bid="110", ask="111")
    refs = {
        k: uuid4()
        for k in (
            "buy_snapshot_id",
            "sell_snapshot_id",
            "buy_fee_version_id",
            "sell_fee_version_id",
        )
    }
    return SimpleNamespace(
        id=uuid4(),
        created_at=when,
        evaluated_at=when,
        **refs,
        evidence={
            "clock": {"trusted": True},
            "buy_book": buy.model_dump(mode="json"),
            "sell_book": sell.model_dump(mode="json"),
            "calculation_inputs": {
                "evaluated_at": when.isoformat(),
                "buy": dump_input(execution.inputs(buy, "synthetic-buy")),
                "sell": dump_input(execution.inputs(sell, "synthetic-sell")),
            },
        },
    )


def test_sized_pair_funds_exactly_both_native_reservations_and_respects_inventory():
    strategy = presets()["Aggressive Research"].model_copy(update={"sell_fee_currency": "base"})
    pair = read_pair(pinned(), AT, strategy)
    point = size_at_budget(pair, D("500"), at_least=True)
    assert point is not None and point.acquisition_cad >= 500
    assert point.buy_quote > point.evaluation.buy_quote_required
    assert point.sell_base > point.evaluation.sell_quantity
    capped = size_at_budget(pair, D("500"), buy_available=D("7"), sell_available=point.sell_base)
    assert capped is None or capped.acquisition_cad < 50
    assert (
        size_at_budget(pair, D("500"), buy_available=point.buy_quote, sell_available=D("0")) is None
    )
    under_cap = size_at_budget(
        pair, D("500"), buy_available=point.buy_quote, sell_available=point.sell_base
    )
    assert under_cap and under_cap.reserved_acquisition_cad <= 500






