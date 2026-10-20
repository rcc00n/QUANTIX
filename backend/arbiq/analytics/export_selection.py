"""Explicit fallback selection. No source rows/financial transactions are deleted.

All scalar row metadata and decision outputs remain. Only nonessential raw payloads
are reduced, with hashes, strata and counters in the archive. Primary/foreign keys,
timestamps and source row counts remain independently auditable.
"""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from arbiq.analytics.evidence_codec import compact

OPTIONAL_RAW = {"market_quality_events", "market_data_samples", "market_observations"}


class AnalysisSelection:
    def __init__(self, store):
        self.db = store.db
        self.db.executescript(
            "CREATE TABLE important(id TEXT PRIMARY KEY);"
            "CREATE TABLE strata(key TEXT PRIMARY KEY, records INTEGER NOT NULL);"
        )

    def important(self, identifier):
        self.db.execute("INSERT OR IGNORE INTO important VALUES (?)", (str(identifier),))

    def is_important(self, row):
        identifier = str(row.get("id"))
        if self.db.execute("SELECT 1 FROM important WHERE id=?", (identifier,)).fetchone():
            return True
        try:
            if Decimal(str(row.get("net_profit_cad"))) > 0:
                return True
        except (InvalidOperation, TypeError):
            pass
        return False

    def apply(self, table, row, json_columns):
        if table in {"arbitrage_evaluations", "arbitrage_routes"}:
            if self.is_important(row):
                return row, False
            changed = False
            for field in json_columns:
                value = row.get(field)
                if not isinstance(value, dict):
                    continue
                # Captured calculation_inputs, costs, all budgets, fee/FX parameters,
                # confirmations and clock survive. Referenced source books are also
                # exported in full; only redundant raw wrappers are reduced.
                if field == "evidence":
                    value, removed = self.trim_evidence(value)
                    changed |= removed
                elif isinstance(value.get("evidence"), dict):
                    evidence, removed = self.trim_evidence(value["evidence"])
                    value = {**value, "evidence": evidence}
                    changed |= removed
                row[field] = value
            return row, changed
        if table not in OPTIONAL_RAW:
            return row, False
        timestamp = next(
            (row.get(k) for k in ("received_at", "observed_at", "created_at") if row.get(k)),
            "unknown",
        )
        if isinstance(timestamp, datetime):
            timestamp = timestamp.astimezone(UTC).isoformat()
        bucket = str(timestamp)[:13]  # UTC hour, not a fabricated interpolated observation.
        code = str(row.get("code") or row.get("reason") or "unknown").split(":", 1)[0]
        key = compact([table, str(row.get("market_id")), bucket, row.get("state"), code])
        prior = self.db.execute("SELECT records FROM strata WHERE key=?", (key,)).fetchone()
        self.db.execute(
            "INSERT INTO strata VALUES (?,1) ON CONFLICT(key) DO UPDATE SET records=records+1",
            (key,),
        )
        unusual = any(word in code.upper() for word in ("EXTREME", "DIVERGENCE", "CROSSED"))
        if prior is None or unusual:
            return row, False
        changed = False
        for field in json_columns:
            original = row.get(field)
            if original is None:
                continue
            scalars = {}
            if isinstance(original, dict):
                scalars = {k: v for k, v in original.items() if not isinstance(v, (dict, list))}
            row[field] = {
                "$quantix_omitted_raw": True,
                "source_sha256": hashlib.sha256(compact(original).encode()).hexdigest(),
                "scalar_summary": scalars,
                "stratum": key,
                "reason": "SIZE_BUDGET_HOURLY_REPRESENTATIVE_RAW",
            }
            changed = True
        return row, changed

    @staticmethod
    def trim_evidence(value):
        removed = {
            k: v
            for k, v in value.items()
            if k in {"buy_book", "sell_book", "fx_books", "raw_evidence"}
        }
        if not removed:
            return value, False
        result = {k: v for k, v in value.items() if k not in removed}
        result["$quantix_omitted_raw"] = {
            "keys": sorted(removed),
            "source_sha256": hashlib.sha256(compact(removed).encode()).hexdigest(),
            "reason": "REDUNDANT_RAW_WRAPPERS_SIZE_BUDGET",
            "retained": (
                "calculation_inputs, normalized source snapshots, all costs/budgets/decisions"
            ),
        }
        return result, True
