"""SYNTHETIC policy/IPC tests: no PostgreSQL, real exchanges, sockets or host changes."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.collector.worker import Collector, ExchangeRuntime
from arbiq.core.telemetry import ProcessTelemetry
from arbiq.market_data.discovery import normalize_market
from arbiq.opportunities.config import EngineConfig
from arbiq.paper_trading.funding import allocate_weighted
from arbiq.portfolio.contracts import PriceEvidence
from arbiq.settings.terminal import TerminalSettings

NOW = datetime(2026, 11, 22, 6, tzinfo=UTC)
D = Decimal


class FakeSession:
    def __init__(self, owner):
        self.owner = owner

    async def scalar(self, query):
        return self.owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def collector_fixture(token="synthetic-internal-token"):
    events = []

    async def event(*args, **kwargs):
        events.append((args, kwargs))

    store = SimpleNamespace(
        sessions=lambda: FakeSession(uuid4()),
        event=event,
        register_exchange=AsyncMock(return_value=uuid4()),
        retain_runtime_keys=lambda ids: None,
    )
    settings = SimpleNamespace(
        collector=CollectorConfig(exchange_ids=["kraken", "ndax"]),
        admin_username="synthetic-owner",
        collector_internal_token=SimpleNamespace(get_secret_value=lambda: token),
    )
    return Collector(settings, store), events


def market(slug, base="BTC", quote="CAD"):
    return replace(
        normalize_market(
            slug,
            {
                "id": base + quote,
                "symbol": base + "/" + quote,
                "base": base,
                "quote": quote,
                "spot": True,
                "active": True,
                "precision": {"amount": "0.0001", "price": "0.01"},
            },
            {},
            precision_mode=4,
        ),
        id=uuid4(),
    )


async def test_saved_market_policy_applies_canonical_filters_caps_and_version(
    monkeypatch,
):
    collector, events = collector_fixture()
    collector.scanner = SimpleNamespace(config=EngineConfig(), current={})
    for slug in ("kraken", "ndax"):
        collector.runtimes[slug] = ExchangeRuntime(
            slug,
            SimpleNamespace(),
            catalog=[
                market(slug, "BTC"),
                market(slug, "ETH"),
                market(slug, "BTC", "USD"),
            ],
        )
    value = TerminalSettings.model_validate(
        {
            "market": {
                "enabled_exchanges": ["kraken"],
                "enabled_assets": ["asset:BTC"],
                "preferred_quote_currencies": ["CAD"],
                "maximum_markets_per_exchange": 2,
                "maximum_age_seconds": 12,
            }
        }
    )
    calls = []

    async def read(db, user_id):
        calls.append(user_id)
        return SimpleNamespace(version=7), value

    monkeypatch.setattr("arbiq.settings.terminal.read_effective_terminal", read)
    await collector.reload_terminal_preferences()
    assert collector.applied_terminal_version == 7
    assert collector.cache.limit == 4
    assert collector.cache.config.maximum_age_seconds == 12
    assert collector.scanner.config.maximum_book_age_seconds == 12
    selected = collector.monitoring_allocation()
    assert [m.base.key for m in selected["kraken"]] == ["asset:BTC"]
    assert selected["ndax"] == []
    assert selected["kraken"][0].quote.key == "fiat:CAD"
    assert calls[0] is not None and len(events) == 1
    previous = collector.config
    await collector.reload_terminal_preferences()
    assert collector.config is previous and len(events) == 1


async def test_environment_defaults_survive_until_owner_saves(monkeypatch):
    collector, events = collector_fixture()
    original = collector.config

    async def read(db, user_id):
        return None, TerminalSettings()

    monkeypatch.setattr("arbiq.settings.terminal.read_effective_terminal", read)
    await collector.reload_terminal_preferences()
    assert collector.config is original and not events
    assert collector.applied_terminal_version == 0


async def test_disabled_exchange_does_not_poll_or_discover():
    collector, _ = collector_fixture()
    connector = SimpleNamespace(
        adapter=SimpleNamespace(id="synthetic-disabled", name="SYNTHETIC", has={}),
        close=AsyncMock(),
        discover_spot_markets=AsyncMock(side_effect=AssertionError("No polling")),
    )
    collector.runtimes["ndax"] = ExchangeRuntime("ndax", connector)
    collector.terminal_market_preferences = {"enabled_exchanges": ["kraken"]}
    task = asyncio.create_task(collector.run_exchange("ndax"))
    await asyncio.sleep(0)
    collector.stop.set()
    await task
    connector.discover_spot_markets.assert_not_called()
    connector.close.assert_awaited_once()






@pytest.fixture
async def private_app(monkeypatch, request):
    collector, _ = collector_fixture(getattr(request, "param", "synthetic-internal-token"))
    captured = []
    ready = asyncio.Event()

    class Runner:
        def __init__(self, app, **kwargs):
            captured.append(app)

        async def setup(self):
            return None

        async def cleanup(self):
            return None

    class Site:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            ready.set()

    monkeypatch.setattr("arbiq.collector.worker.web.AppRunner", Runner)
    monkeypatch.setattr("arbiq.collector.worker.web.TCPSite", Site)
    task = asyncio.create_task(collector.serve())
    await ready.wait()
    try:
        yield collector, captured[0]
    finally:
        collector.stop.set()
        await task


async def invoke(
    app, path, payload=None, *, auth="Bearer synthetic-internal-token", query=None
):
    handler = next(
        route.handler
        for route in app.router.routes()
        if route.resource.canonical == path
    )

    async def body():
        return payload

    request = SimpleNamespace(
        path=path, headers={"Authorization": auth}, query=query or {}, json=body
    )
    response = await app.middlewares[0](request, handler)
    return response.status, json.loads(response.body)
















