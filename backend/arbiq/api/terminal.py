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


def summary(data):
    feeds = data["feeds"]
    connected = [
        x
        for x in feeds
        if x["enabled"]
        and x["collector_healthy"]
        and x["public_data_verified"]
        and x["state"] != "UNAVAILABLE"
        and x.get("monitored_count", 0) > 0
    ]
    scanner = data.get("scanner", {})
    metrics = scanner.get("metrics", {})
    warnings = []
    clock = scanner.get("clock", {})
    if not clock.get("trusted"):
        warnings.append("TRUSTED_UTC_UNAVAILABLE")
    if not clock.get("host_healthy"):
        warnings.append("HOST_CLOCK_UNHEALTHY")
    if not data.get("collector", {}).get("healthy"):
        warnings.append("COLLECTOR_UNAVAILABLE")
    if not data.get("paper", {}).get("healthy"):
        warnings.append("PAPER_COORDINATOR_UNAVAILABLE")
    if metrics.get("pending_comparisons", 0):
        warnings.append("SCANNER_COMPARISON_BACKLOG")
    warnings.append("TRADING_ELIGIBILITY_AND_FEES_UNVERIFIED")
    return {
        "schema_version": "terminal-v1",
        "as_of": data.get("as_of"),
        "connected_exchanges": len(connected),
        "venue_groups": len({venue_group(x["slug"]) for x in connected}),
        "discovered_markets": sum(x["market_count"] for x in feeds if x["enabled"]),
        "monitored_markets": metrics.get("monitored_markets", 0),
        "compatible_intersections": metrics.get("compatible_intersections", 0),
        "collector": data.get("collector", {}),
        "scanner": scanner,
        "paper": data.get("paper", {}),
        "feeds": feeds,
        "warnings": warnings,
        "refresh": {
            "recommended_seconds": 15,
            "runtime_cache_seconds": 5,
            "transport": "bounded_authenticated_polling",
        },
    }


@router.get("/overview")
async def overview(request: Request, db: DB, user: AuthenticatedUser):
    value = summary(await runtime(request, db, user))
    config = request.app.state.settings.arbitrage
    routes = list(
        await db.scalars(select(ArbitrageRoute).order_by(ArbitrageRoute.updated_at.desc()).limit(8))
    )
    # Summary uses retained projections, never promotes these into fresh quotes.
    remove = {"evidence", "costs", "budget_results", "maximum_executable", "transfer_scenario"}
    value["recent_opportunities"] = [
        {
            k: v
            for k, v in project(
                row, config.default_budget_cad, value["scanner"], config.maximum_book_age_seconds
            ).items()
            if k not in remove
        }
        for row in routes
    ]
    trades = list(
        await db.scalars(
            select(PaperTrade)
            .join(PaperSession)
            .where(PaperSession.user_id == user.id)
            .order_by(PaperTrade.requested_at.desc())
            .limit(8)
        )
    )
    value["recent_simulations"] = [trade_dto(x) for x in trades]
    return value


@router.get("/health")
async def health(request: Request, db: DB, user: AuthenticatedUser):
    data = await runtime(request, db, user)
    value = summary(data)
    revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    events = list(
        await db.scalars(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(30))
    )
    sampler = request.app.state.process_telemetry
    latency = list(request.app.state.http_latencies)
    delivery_rows = list(
        await db.scalars(
            select(NotificationEvent.details)
            .where(NotificationEvent.event_type == "delivery.delivered")
            .order_by(NotificationEvent.created_at.desc())
            .limit(600)
        )
    )
    return {
        **value,
        "database": "connected",
        "schema_revision": revision,
        "uptime_seconds": sampler.sample()["uptime_seconds"],
        "telemetry": {
            "api": sampler.sample(),
            "api_memory_maintenance": request.app.state.memory_maintenance,
            "collector": data.get("telemetry"),
            "postgres_process": None,
            "frontend_process": None,
            "unavailable_reason": "No Docker socket or host-agent access granted to application",
        },
        "api_response_latency_ms": {
            **distribution(latency),
            "sample_count": len(latency),
            "mean": round(mean(latency), 3) if latency else None,
            "peak": round(max(latency), 3) if latency else None,
            "window_limit": 1000,
        },
        "notification_timings": {
            "queue_to_provider_ack_ms": distribution(
                row.get("queue_to_provider_ack_ms") for row in delivery_rows
            ),
            "provider_request_ms": distribution(
                row.get("request_duration_ms") for row in delivery_rows
            ),
            "scope": "last_600_retained_delivered_events; no device-receipt measurement",
        },
        "events": [
            {
                "id": str(x.id),
                "created_at": x.created_at,
                "event_type": x.event_type,
                "severity": x.severity,
                "message": x.message,
            }
            for x in events
        ],
    }


def initialize_terminal(app):
    app.state.terminal_cache = TerminalCache()
    app.state.process_telemetry = ProcessTelemetry()
    app.state.http_latencies = deque(maxlen=1000)
