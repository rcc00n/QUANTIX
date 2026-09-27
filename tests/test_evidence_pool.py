"""SYNTHETIC evidence only; sharing must not change financial provenance."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.collector.config import CollectorConfig
from arbiq.market_data.contracts import BookLevel
from arbiq.market_data.quality import safe_json, validate_book
from arbiq.opportunities.analysis import dump_input, load_input
from arbiq.opportunities.contracts import Fee, MarketInput, MarketRules, identity_cad
from arbiq.opportunities.evidence import (
    EvidencePool,
    FrozenJSON,
    freeze_json,
    shared_json,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def synthetic_book(market_id=None, *, received_at=NOW, price="100"):
    return validate_book(
        {
            "symbol": "SYNTH/CAD",
            "timestamp": int(received_at.timestamp() * 1000),
            "bids": [[str(Decimal(price) - i - 1), "10"] for i in range(3)],
            "asks": [[str(Decimal(price) + i), "10"] for i in range(3)],
        },
        market_id=market_id or uuid4(),
        exchange="synthetic",
        symbol="SYNTH/CAD",
        base_key="synthetic:asset",
        quote_key="fiat:CAD",
        received_at=received_at,
        request_started_at=received_at,
        latency_ms=0,
        config=CollectorConfig(),
    )


def synthetic_input(mid=None):
    return MarketInput(
        str(mid or uuid4()),
        "synthetic-venue",
        "synthetic:asset",
        "CAD",
        (BookLevel(price="100", quantity="10"),),
        (BookLevel(price="99", quantity="10"),),
        Fee(Decimal("0.006")),
        MarketRules(amount_step=Decimal("0.001")),
        identity_cad(),
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_book_and_full_input_are_reused_only_when_unchanged():
    pool = EvidencePool(2)
    book = synthetic_book()
    cached = pool.book(book)
    assert pool.book(book) is cached
    changed = synthetic_book(book.market_id, received_at=NOW + timedelta(seconds=1))
    assert pool.book(changed) is not cached
    assert cached["received_at"] == NOW.isoformat().replace("+00:00", "Z")
    value = synthetic_input(book.market_id)
    serialized = pool.market_input(value)
    assert pool.market_input(replace(value)) is serialized
    assert load_input(serialized) == value
    assert (
        pool.market_input(replace(value, fee=Fee(Decimal("0.007")))) is not serialized
    )
    assert canonical(cached) == canonical(safe_json(book.model_dump(mode="json")))


def test_shared_json_is_exactly_equivalent_and_immutable():
    pool = EvidencePool(2)
    book = synthetic_book()
    source = synthetic_input(book.market_id)
    payload = {
        "quantity": Decimal("0.001"),
        "utc": NOW,
        "evidence": {
            "fx_books": {str(book.market_id): pool.book(book)},
            "calculation_inputs": {
                "buy": pool.market_input(source),
                "sell": pool.market_input(source),
            },
        },
    }
    original = safe_json(payload)
    shared = pool.current(shared_json(payload), payload)
    assert canonical(shared) == canonical(original)
    assert (
        shared["evidence"]["calculation_inputs"]["buy"]
        is shared["evidence"]["calculation_inputs"]["sell"]
    )
    with pytest.raises(TypeError, match="immutable"):
        shared["evidence"]["calculation_inputs"]["buy"]["asks"][0]["price"] = "1"
    with pytest.raises(TypeError, match="immutable"):
        shared["evidence"]["fx_books"].clear()
    assert canonical(pool.market_input(source)) == canonical(dump_input(source))


