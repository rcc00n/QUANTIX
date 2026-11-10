"""SYNTHETIC transport fixtures; these tests make no public API connections."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from arbiq.auth.security import hash_password
from arbiq.collector.config import CollectorConfig
from arbiq.collector.scheduling import Schedule
from arbiq.collector.worker import Collector, ExchangeRuntime
from arbiq.connectors import kraken_ws
from arbiq.connectors.kraken_ws import (
    KrakenBook,
    KrakenStream,
    ResynchronizationRequired,
    SequenceGuard,
)
from arbiq.core.config import Settings
from arbiq.market_data.discovery import normalize_market
from test_market_data import synthetic_capture, synthetic_market, synthetic_raw


def synthetic_ws_snapshot():
    return {
        "symbol": "BTC/USD",
        "bids": [{"price": "99", "qty": "10"}, {"price": "98", "qty": "2"}],
        "asks": [{"price": "101", "qty": "3"}, {"price": "102", "qty": "2"}],
        "checksum": 1882172307,
    }


def test_native_checksum_snapshot_delta_and_resynchronization():
    book = KrakenBook(10)
    with pytest.raises(ResynchronizationRequired, match="DELTA_WITHOUT_SNAPSHOT"):
        book.apply(synthetic_ws_snapshot(), snapshot=False)
    value = book.apply(synthetic_ws_snapshot(), snapshot=True)
    assert value["bids"][0][0].as_tuple().exponent == 0
    assert book.checksum() == 1882172307
    # Zero-quantity removal is a legal delta; a wrong checksum invalidates local state.
    with pytest.raises(ResynchronizationRequired, match="CHECKSUM_MISMATCH"):
        book.apply(
            {
                "symbol": "BTC/USD",
                "bids": [{"price": "99", "qty": "0"}],
                "asks": [],
                "checksum": 1,
            },
            False,
        )
    assert not book.initialized
    assert book.apply(synthetic_ws_snapshot(), snapshot=True)["symbol"] == "BTC/USD"
    sequence = SequenceGuard()
    sequence.accept(100, snapshot=True)
    sequence.accept(101)
    with pytest.raises(ResynchronizationRequired, match="SEQUENCE_GAP"):
        sequence.accept(103)
    sequence.accept(500, snapshot=True)
    sequence.accept(501)




def settings(config):
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://synthetic:synthetic@db/quantix_test",
        admin_password_hash=hash_password("synthetic-password-only"),
        collector=config,
    )














