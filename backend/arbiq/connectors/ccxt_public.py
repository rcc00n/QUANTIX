"""Read-only CCXT facade. Financial JSON is decoded without binary floats."""

import asyncio
import hashlib
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

import aiohttp
import ccxt
import ccxt.async_support as async_ccxt

from arbiq.collector.config import CollectorConfig
from arbiq.connectors.metadata import MetadataArchive, publish
from arbiq.core.clock import elapsed_seconds


class PublicConnector:
    def __init__(self, exchange_id: str, config: CollectorConfig):
        if exchange_id not in async_ccxt.exchanges:
            raise ValueError("Unsupported public adapter")
        self.exchange_id = exchange_id
        self.config = config
        self.adapter = getattr(async_ccxt, exchange_id)(
            {
                "enableRateLimit": True,
                "timeout": int(config.request_timeout_seconds * 1000),
                "options": {"defaultType": "spot"},
                "enableLastHttpResponse": False,
                "enableLastJsonResponse": False,
            }
        )
        discovery = self.adapter.options.get("fetchMarkets")
        if isinstance(discovery, dict):
            types = discovery.get("types")
            if isinstance(types, list) and "spot" in types:
                self.adapter.options["fetchMarkets"] = {**discovery, "types": ["spot"]}
            elif isinstance(types, dict) and "spot" in types:

                def disable(value):
                    return (
                        {key: disable(item) for key, item in value.items()}
                        if isinstance(value, dict)
                        else False
                    )

                self.adapter.options["fetchMarkets"] = {
                    **discovery,
                    "types": {
                        key: value if key == "spot" else disable(value)
                        for key, value in types.items()
                    },
                }
        original_safe_string = self.adapter.safe_string

        def decimal_safe_string(dictionary, key, default_value=None):
            try:
                value = dictionary[key]
                if isinstance(value, Decimal):
                    return str(value)
            except (KeyError, IndexError, TypeError):
                pass
            return original_safe_string(dictionary, key, default_value)

        # CCXT safe_string deliberately excludes Decimal. Preserve precision in
        # parsed currency metadata as well as JSON prices (Kraken regression).
        self.adapter.safe_string = decimal_safe_string
        self.adapter.number = Decimal

        def exact_level(row, priceKey=0, amountKey=1, countOrIdKey=2):
            # CCXT 4.5.93 base L2 parser calls safe_float regardless of number.
            # Replace this public-book hook so prices/quantities never pass through floats.
            return [Decimal(str(row[priceKey])), Decimal(str(row[amountKey]))]

        self.adapter.parse_order_book_bid_ask = exact_level
        if exchange_id == "kraken":
            original_book_parser = self.adapter.parse_order_book

            def kraken_book_timestamp(raw, *args, **kwargs):
                result = original_book_parser(raw, *args, **kwargs)
                timestamps = [
                    Decimal(str(row[2]))
                    for side in ("bids", "asks")
                    for row in raw.get(side, [])
                    if len(row) > 2
                ]
                if result.get("timestamp") is None and timestamps:
                    result["timestamp"] = int(max(timestamps) * 1000)
                    result["timestamp_basis"] = "latest_level_update"
                return result

            self.adapter.parse_order_book = kraken_book_timestamp
        self.last_public_excerpt = ""
        self.last_public_sha256 = ""
        self.retry_after_seconds = 0.0

        def capture_public_response(
            status, status_text, url, method, headers, body, request_headers, request_body
        ):
            self._body_completed = elapsed_seconds()
            self.last_timing["response_completed_at"] = self.utcnow().isoformat()
            self.last_public_excerpt = body[:8192]
            self.last_public_sha256 = hashlib.sha256(body.encode()).hexdigest()
            self.retry_after_seconds = 0.0
            if status in (418, 429):
                value = headers.get("Retry-After", headers.get("retry-after", ""))
                try:
                    self.retry_after_seconds = max(0, float(value))
                except (ValueError, TypeError):
                    try:
                        self.retry_after_seconds = max(
                            0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                        )
                    except (ValueError, TypeError):
                        pass
            return body

        self.adapter.on_rest_response = capture_public_response
        self.adapter.on_json_response = lambda raw: json.loads(raw, parse_float=Decimal)
        self._request_gate = None
        self._metadata_gate = None
        self._gate = asyncio.Lock()
        self._last_request = 0.0
        self._session = None
        self.last_latency_ms: int | None = None
        self.utcnow = lambda: datetime.now(UTC)  # Collector injects TrustedClock before opening.
        self.last_timing = {}
        self._http_started = None
        self._body_completed = None
        self.metadata_archive: MetadataArchive | None = None
        self.metadata_focus: tuple[str, ...] | None = None
        self.metadata_metrics = {"state": "not_archived"}

    async def open(self):
        if self._session is None:
            trace = aiohttp.TraceConfig()

            async def request_started(session, context, params):
                self._http_started = elapsed_seconds()
                self.last_timing["http_request_started_at"] = self.utcnow().isoformat()

            trace.on_request_start.append(request_started)
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
                timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_seconds),
                trace_configs=[trace],
            )
            self.adapter.session = self._session

    async def _call(self, function, *args, deadline_seconds=None, **kwargs):
        await self.open()
        queued = elapsed_seconds()
        async with self._gate:
            acquired = elapsed_seconds()
            self.last_timing = {"connector_queue_ms": (acquired - queued) * 1000}
            self._http_started = self._body_completed = None
            delay = self.config.minimum_request_interval_seconds - (
                time.monotonic() - self._last_request
            )
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_timing["pacing_wait_ms"] = (elapsed_seconds() - acquired) * 1000
            gate_started = elapsed_seconds()
            async with (
                self._metadata_gate
                if deadline_seconds and self._metadata_gate is not None
                else nullcontext(),
                self._request_gate if self._request_gate is not None else nullcontext(),
            ):
                self.last_timing["global_queue_ms"] = (elapsed_seconds() - gate_started) * 1000
                self._last_request = time.monotonic()
                async with asyncio.timeout(
                    deadline_seconds or self.config.request_timeout_seconds + 5
                ):
                    started = elapsed_seconds()
                    result = await function(*args, **kwargs)
                    parsed = elapsed_seconds()
                    self.last_latency_ms = round((parsed - started) * 1000)
                    self.last_timing.update(
                        adapter_parsed_at=self.utcnow().isoformat(),
                        request_ms=(parsed - started) * 1000,
                        http_body_ms=(self._body_completed - self._http_started) * 1000
                        if self._body_completed is not None and self._http_started is not None
                        else None,
                        adapter_parse_ms=(parsed - self._body_completed) * 1000
                        if self._body_completed is not None
                        else None,
                    )
                    return result

