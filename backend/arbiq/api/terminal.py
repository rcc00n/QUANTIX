"""Authenticated bounded terminal summaries; no public/private credential telemetry."""

import asyncio
from collections import deque
from statistics import mean

import aiohttp
from fastapi import APIRouter, Request
from sqlalchemy import select, text

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.core.clock import elapsed_seconds
from arbiq.core.telemetry import ProcessTelemetry
from arbiq.db.models import ArbitrageRoute, PaperSession, PaperTrade, SystemEvent
from arbiq.market_data.api import exchanges
from arbiq.market_data.cache import venue_group
from arbiq.market_data.timing import distribution
from arbiq.notifications.models import NotificationEvent
from arbiq.opportunities.api import collector_diagnostics, project
from arbiq.paper_trading.coordinator import trade_dto

router = APIRouter(prefix="/terminal", tags=["terminal telemetry"])


class TerminalCache:
    """Five-second singleflight cache contains only venue/runtime public data."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.until = 0.0
        self.value = None


async def runtime(request, db, user):
    cache = request.app.state.terminal_cache
    async with cache.lock:
        if cache.value is not None and elapsed_seconds() < cache.until:
            return cache.value
        client = request.app.state.collector_http
        data = {}
        if client is not None:
            try:
                async with client.get("/terminal/runtime") as response:
                    if response.status == 200:
                        data = await response.json()
            except (aiohttp.ClientError, TimeoutError, ValueError):
                pass
        if not isinstance(data, dict) or not data:
            if hasattr(request, "state"):
                request.state.collector_unavailable = True
            data = {
                "as_of": None,
                "collector": {"healthy": False, "source": "unavailable"},
                "paper": {"healthy": False, "source": "unavailable"},
                "scanner": await collector_diagnostics(request, db),
                "telemetry": None,
            }
        data["feeds"] = await exchanges(request, db, user)
        cache.value, cache.until = data, elapsed_seconds() + 5
        return data


def initialize_terminal(app):
    app.state.terminal_cache = TerminalCache()
    app.state.process_telemetry = ProcessTelemetry()
    app.state.http_latencies = deque(maxlen=1000)
