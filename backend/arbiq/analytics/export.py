"""Self-contained bounded ZIP exports of genuine records, never generated conclusions."""

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select

from arbiq.analytics.contracts import (
    CSV_COLUMNS,
    EXPORT_FILES,
    AnalyticsExportManifest,
    AnalyticsExportMetrics,
)
from arbiq.analytics.service import analytics
from arbiq.db.models import (
    ArbitrageEvaluation,
    ArbitrageLifecycleEvent,
    ArbitrageRoute,
    Asset,
    Exchange,
    FeeVersion,
    FXQuote,
    InventoryTransfer,
    Journal,
    LedgerEntry,
    Market,
    NotificationAttempt,
    Opportunity,
    OrderBookSnapshot,
    PaperFill,
    PaperOrder,
    PaperTrade,
    Signal,
    SimulationEvent,
    StrategyVersion,
    SystemEvent,
    VirtualAccount,
    VirtualDeposit,
)
from arbiq.notifications.models import AlertDecision, NotificationEvent, NotificationOutbox
from arbiq.opportunities.contracts import CALCULATION_VERSION
from arbiq.paper_trading.lifecycle import lifecycle_rank
from arbiq.paper_trading.models import ExecutionObservation
from arbiq.portfolio.contracts import ACCOUNTING_VERSION
from arbiq.portfolio.models import AccountingMovement, PortfolioCheckpoint
from arbiq.settings.models import TerminalSettingsVersion

# Wide immutable evaluation/book JSON can be hundreds of KiB per row. Bound the
# decoded cursor batch independently of the total export record/byte limits.
BATCH_ROWS = 32
MAX_ROWS_PER_FILE = 100_000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_GENERATION_SECONDS = 90
SECRET_KEYS = re.compile(
    r"(^|_)(password|passwd|secret|cookie|authorization|auth|api_key|apikey|chat_id|chatid|"
    r"access_token|refresh_token|bot_token|session_token|api_token|id_token|"
    r"private_key|credentials?)(_|$)|^token$",
    re.I,
)
SECRET_QUERY = re.compile(r"(?i)(password|api[_-]?key|token|authorization)=([^\s&]+)")
URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", re.I)


def exact(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {str(k): exact(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [exact(v) for v in value]
    return value


