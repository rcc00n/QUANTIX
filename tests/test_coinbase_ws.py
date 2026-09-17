"""SYNTHETIC Coinbase Exchange wire fixtures. No socket, trade or credential use."""

import json
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from arbiq.connectors import coinbase_ws
from arbiq.connectors.coinbase_ws import CoinbaseProtocol
from arbiq.connectors.stream_protocol import StreamInvalid

NOW = datetime(2026, 3, 4, tzinfo=UTC)
PRODUCT = "BTC-USD"
OTHER_PRODUCT = "ETH-USD"


def send(protocol, payload):
    return protocol.consume(json.dumps(payload).encode())


def snapshot(**changes):
    return {
        "type": "snapshot",
        "product_id": PRODUCT,
        "bids": [["100.00", "1.000000000000000001"], ["98.00", "2"]],
        "asks": [["103.00", "2"], ["102.00", "3"]],
        **changes,
    }


def delta(changes, step=1, **fields):
    return {
        "type": "l2update",
        "product_id": PRODUCT,
        "time": (NOW + timedelta(microseconds=step)).isoformat(),
        "changes": changes,
        **fields,
    }


def acknowledgement(products):
    return {
        "type": "subscriptions",
        "channels": [
            {"name": "level2_50", "product_ids": products},
            {"name": "heartbeat", "product_ids": products},
        ],
    }


def heartbeat(sequence):
    return {
        "type": "heartbeat",
        "product_id": PRODUCT,
        "time": NOW.isoformat(),
        "sequence": sequence,
        "last_trade_id": 40,
    }


def test_native_product_ids_and_batch_subscription_alias():
    protocol = CoinbaseProtocol([OTHER_PRODUCT, PRODUCT], 25)
    assert protocol.url == "wss://ws-feed.exchange.coinbase.com"
    assert protocol.ping_message is None
    assert protocol.subscription() == {
        "type": "subscribe",
        "product_ids": [PRODUCT, OTHER_PRODUCT],
        "channels": ["level2_batch", "heartbeat"],
    }
    ack = send(protocol, acknowledgement([PRODUCT, OTHER_PRODUCT]))
    assert ack.activity and not ack.books and not ack.synchronized
    with pytest.raises(StreamInvalid, match="INCOMPLETE_SUBSCRIPTION"):
        send(protocol, acknowledgement([PRODUCT]))
    with pytest.raises(ValueError, match="native Exchange product IDs"):
        CoinbaseProtocol(["BTC/USD"], 25)












