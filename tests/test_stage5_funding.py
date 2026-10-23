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














