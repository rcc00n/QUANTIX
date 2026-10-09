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








