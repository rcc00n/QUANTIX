"""Incremental, bounded scanner embedded in the singleton public collector.

Only normalized, monitored identities enter the index; the discovered catalog is
never quadratically compared. This module never submits orders or creates fills.
"""

import asyncio
import hashlib
import time
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from arbiq.core.clock import elapsed_seconds
from arbiq.market_data.quality import DataState, safe_json
from arbiq.market_data.timing import Timings, age_seconds
from arbiq.opportunities.contracts import (
    CALCULATION_VERSION,
    ConversionRoute,
    Costs,
    Fee,
    FXConversion,
    MarketInput,
    MarketRules,
    identity_cad,
)
from arbiq.opportunities.coverage import metrics as coverage_metrics
from arbiq.opportunities.coverage import venue_group
from arbiq.opportunities.evidence import EvidencePool, shared_json
from arbiq.opportunities.math import conversion_leg_from_book, optimize_route
from arbiq.opportunities.projection_cache import ProjectionCache
from arbiq.opportunities.storage import OpportunityStore

ANOMALY_REASONS = {
    "extreme_reference_discrepancy",
    "extreme_temporal_discrepancy",
    "unconfirmed_anomaly",
}


def book_reasons(book, now, config):
    reasons = []
    age = (now - book.received_at).total_seconds()
    if age < 0 or age > config.maximum_book_age_seconds:
        reasons.append("QUOTE_INGESTION_NOT_CURRENT")
    if book.exchange_timestamp is None:
        reasons.append("SOURCE_TIMESTAMP_UNAVAILABLE")
    elif (
        not 0 <= (now - book.exchange_timestamp).total_seconds() <= config.maximum_book_age_seconds
    ):
        reasons.append("SOURCE_TIMESTAMP_NOT_CURRENT")
    reasons.extend(book.reasons)
    if book.state in {DataState.STALE, DataState.UNAVAILABLE}:
        reasons.append("BOOK_" + book.state.value)
    if book.state == DataState.SUSPICIOUS:
        reasons.append("ANOMALY_REQUIRES_FRESH_INDEPENDENT_RECHECK")
    if book.verification.get("last_error_code"):
        reasons.append("EXCHANGE_LAST_REQUEST_FAILED")
    return list(dict.fromkeys(reasons))


