"""SYNTHETIC terminal state and prices; never claim live verification from fixtures."""
# ruff: noqa: F811 -- Pytest discovers the imported isolated fixture by name.

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from arbiq.api import terminal
from arbiq.connectors.market_links import official_market_link
from arbiq.core.telemetry import ProcessTelemetry
from arbiq.opportunities.api import meets_filters
from arbiq.portfolio.analytics import summarize_trades
from test_paper_api import private_api  # noqa: F401


def fixture_bundle():
    def feed(slug):
        return {
            "enabled": True,
            "collector_healthy": True,
            "public_data_verified": True,
            "state": "DEGRADED",
            "slug": slug,
            "market_count": 100,
            "monitored_count": 1,
        }

    return {
        "as_of": "2025-01-01T00:00:00Z",
        "collector": {"healthy": True, "quality_counts": {"DEGRADED": 3}},
        "scanner": {
            "healthy": True,
            "clock": {"trusted": True, "host_healthy": False},
            "metrics": {"monitored_markets": 3, "compatible_intersections": 1},
        },
        "paper": {"healthy": True},
        "telemetry": None,
        "feeds": [feed("coinbase"), feed("coinbaseexchange"), feed("kraken")],
    }


def test_terminal_authentication_and_real_empty_states(
    client, private_api, monkeypatch
):
    # Auth fixture is a SYNTHETIC user; only runtime metadata is mocked.
    monkeypatch.setattr(terminal, "runtime", AsyncMock(return_value=fixture_bundle()))
    authenticated, _, _ = private_api
    overview = authenticated.get("/api/v1/terminal/overview")
    assert overview.status_code == 200
    value = overview.json()
    assert value["connected_exchanges"] == 3
    assert value["venue_groups"] == 2
    assert value["monitored_markets"] == 3
    assert "HOST_CLOCK_UNHEALTHY" in value["warnings"]
    assert len(value["recent_opportunities"]) <= 8
    assert len(value["recent_simulations"]) <= 8
    health = authenticated.get("/api/v1/terminal/health").json()
    assert health["database"] == "connected"
    assert health["schema_revision"] == "0010_notifications"
    assert health["telemetry"]["api"]["current_rss_bytes"] > 0
    assert health["telemetry"]["postgres_process"] is None
    assert "password_hash" not in str(health)
    client.cookies.clear()
    assert client.get("/api/v1/terminal/health").status_code == 401
    assert client.get("/api/v1/terminal/overview").status_code == 401


def test_runtime_singleflight_is_bounded_and_contains_no_portfolio(monkeypatch):
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            calls.append(1)
            await asyncio.sleep(0)
            return {k: v for k, v in fixture_bundle().items() if k != "feeds"}

    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                terminal_cache=terminal.TerminalCache(),
                collector_http=SimpleNamespace(get=lambda _: Response()),
            )
        )
    )
    monkeypatch.setattr(
        terminal, "exchanges", AsyncMock(return_value=fixture_bundle()["feeds"])
    )

    async def check():
        values = await asyncio.gather(
            *(terminal.runtime(req, None, None) for _ in range(20))
        )
        assert len(calls) == 1
        assert all(
            "portfolio" not in x and "recent_simulations" not in x for x in values
        )
        assert req.app.state.terminal_cache.until > 0

    asyncio.run(check())


def test_exact_candidate_filters_never_round_or_accept_missing_values():
    row = {
        "base_asset": "SYNTH",
        "buy_exchange": "synthetic_buy",
        "data_confidence": "SUSPICIOUS",
        "net_profit_cad": "0.000000000000000001",
        "net_return_pct": "-0.1",
        "quantity": "0.000000000000000001",
        "maximum_liquidity_cad": "100.000000000000000001",
        "acquisition_cost_cad": "50",
    }
    assert meets_filters(
        row,
        search="SYNTHETIC",
        min_profit_cad=Decimal("1e-18"),
        confidence="SUSPICIOUS",
        min_liquidity_cad=Decimal(100),
    )
    assert not meets_filters(row, min_profit_cad=Decimal("2e-18"))
    assert not meets_filters(row, min_return_pct=Decimal(0))
    assert not meets_filters(row, max_acquisition_cad=Decimal(49))
    assert not meets_filters(
        {**row, "maximum_liquidity_cad": None}, min_liquidity_cad=Decimal(1)
    )
    assert not meets_filters(
        {**row, "net_profit_cad": "NaN"}, min_profit_cad=Decimal(0)
    )






