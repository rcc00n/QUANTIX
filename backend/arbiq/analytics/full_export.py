"""Complete retained-data snapshot. Explicit scopes, exact values, bounded cursors/disk.

This is a separate envelope from the stable custom quantix-export-v1 contract.
No financial records or market evidence are modified by an export.
"""

import csv
import hashlib
import io
import itertools
import json
import re
import shutil
import time
import zipfile
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import JSON, func, or_, select, text

from arbiq.analytics.evidence_codec import CODEC, EvidenceStore, compact
from arbiq.analytics.export import cell, exact, sanitize, secret_key, sequential_thread
from arbiq.analytics.export_budget import (
    MAX_TEMP_BYTES,
    MAX_ZIP_BYTES,
    ExportBudgetExceeded,
    ExportCancelled,
)
from arbiq.analytics.export_selection import AnalysisSelection
from arbiq.analytics.export_views import (
    OPPORTUNITY_COLUMNS,
    PORTFOLIO_COLUMNS,
    readable_projection,
    write_coverage,
)
from arbiq.db.base import Base
from arbiq.market_data.timing import TIMING_EXPORT_NOTES

SCHEMA = "quantix-full-v2"
BATCH = 8
MIN_FREE_BYTES = 6 * 1024**3
MAX_OUTPUT_BYTES = 24 * 1024**3

# New tables must receive an explicit policy; schema growth must never silently
# omit data or accidentally export authentication/private communication records.
GROUPS = {
    "portfolios": "paper_sessions paper_controls virtual_accounts virtual_deposits "
    "balance_snapshots portfolio_positions funding_experiments funding_proposals",
    "executions": "paper_trades paper_orders paper_fills simulation_events "
    "execution_observations inventory_transfers paper_rebalance_requests "
    "journals ledger_entries accounting_movements actual_trades",
    "opportunities": "opportunities arbitrage_routes arbitrage_evaluations "
    "arbitrage_lifecycle_events",
    "market_data": "assets market_observations order_book_snapshots fx_quotes "
    "latest_market_books market_data_samples market_quality_events",
    "exchanges": "exchanges markets exchange_config_versions exchange_health fee_versions",
    "signals": "signals notification_attempts alert_decisions alert_lifecycle "
    "notification_outbox notification_events",
    "analytics": "portfolio_valuations portfolio_checkpoints daily_accounting_snapshots",
    "settings": "strategy_versions strategy_heads terminal_settings_versions",
    "system_health": "system_events collector_runs report_exports",
}
TABLE_GROUP = {name: group for group, names in GROUPS.items() for name in names.split()}
EXCLUDED_TABLES = {
    "users": "Authentication and personal identity; never exported.",
    "auth_sessions": "Session cookies/hashes; never exported.",
    "login_attempts": "Login identity/client fingerprints; never exported.",
    "telegram_challenges": "Private pairing challenges and identities; never exported.",
    "telegram_bindings": "Private Telegram destinations and identities; never exported.",
    "notification_worker_state": (
        "Private Telegram update offsets/runtime state; health remains in system events."
    ),
}
PRIVATE_COLUMNS = {
    "user_id",
    "actor_user_id",
    "acknowledged_by",
    "binding_id",
    "message",
    "provider_message_id",
    "user_reference",
    "idempotency_key",
    "deduplication_key",
}
PRIVATE_KEYS = re.compile(
    r"(^|_)(user_?id|username|email|phone|chat_?id|telegram|binding_?id|destination|"
    r"message|text|acknowledged_by|actor_user_id|user_reference|client_hash)(_|$)",
    re.I,
)
BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


@lru_cache(maxsize=2048)
def private_key(key):
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.replace("-", "_"))
    return bool(PRIVATE_KEYS.search(normalized)) or secret_key(key)


def public_value(value):
    """Apply the established secret policy plus private identity/message exclusions."""
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if private_key(str(k)) else public_value(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [public_value(v) for v in value]
    if isinstance(value, (Decimal, datetime, UUID)):
        return exact(value)
    if isinstance(value, str):
        return EMAIL.sub("[REDACTED_EMAIL]", BOT_TOKEN.sub("[REDACTED_TOKEN]", sanitize(value)))
    return value


def public_record(value):
    return public_value(value)


def scope_for(table, owner, seen=frozenset()):
    """Follow financial ownership joins; public scanner/registry evidence is shared."""
    if table.name in seen:
        return None
    if "user_id" in table.c:
        return table.c.user_id == owner
    if table.name == "system_events":
        return or_(table.c.actor_user_id == owner, table.c.actor_user_id.is_(None))
    for column in table.c:
        for fk in column.foreign_keys:
            parent = fk.column.table
            if parent.name in EXCLUDED_TABLES:
                continue
            parent_scope = scope_for(parent, owner, seen | {table.name})
            if parent_scope is not None:
                return column.in_(select(fk.column).where(parent_scope))
    return None


