"""Synthetic OKX state-machine fixtures. These tests use no network or trading APIs."""

import json
from decimal import Decimal

import pytest
from arbiq.connectors.okx_ws import OkxProtocol
from arbiq.connectors.stream_protocol import StreamInvalid

SYMBOL = "BTC-USDT"
SECOND_SYMBOL = "ETH-USDT"
TS = "1760000123456"


def level(price, size):
    return [str(price), str(size), "0", "1"]


def message(
    action="snapshot",
    *,
    symbol=SYMBOL,
    previous=-1,
    sequence=10,
    timestamp=TS,
    bids=None,
    asks=None,
    checksum=0,
):
    return json.dumps(
        {
            "arg": {"channel": "books", "instId": symbol},
            "action": action,
            "data": [
                {
                    "prevSeqId": previous,
                    "seqId": sequence,
                    "ts": timestamp,
                    "bids": [level("99.123456789012345678", "7.10")] if bids is None else bids,
                    "asks": [level("100", "2.5")] if asks is None else asks,
                    "checksum": checksum,
                }
            ],
        }
    )


def test_okx_subscription_and_exact_snapshot_provenance():
    protocol = OkxProtocol([SYMBOL, SECOND_SYMBOL], 25)
    assert protocol.url == "wss://ws.okx.com/ws/v5/public"
    assert protocol.ping_message == "ping"
    assert protocol.subscription() == {
        "op": "subscribe",
        "args": [
            {"channel": "books", "instId": SYMBOL},
            {"channel": "books", "instId": SECOND_SYMBOL},
        ],
    }
    acknowledgement = protocol.consume(
        json.dumps({"event": "subscribe", "arg": {"channel": "books", "instId": SYMBOL}})
    )
    assert not acknowledgement.synchronized and not acknowledgement.activity
    frame = protocol.consume(message())
    assert frame.synchronized == {SYMBOL}
    native, book = frame.books[0]
    assert native == book["symbol"] == SYMBOL
    assert book["bids"] == [[Decimal("99.123456789012345678"), Decimal("7.10")]]
    assert book["asks"] == [[Decimal(100), Decimal("2.5")]]
    assert book["timestamp"] == int(TS) and book["timestamp_basis"] == "exchange_book_event"
    assert book["stream_integrity"] == {
        "channel": "books",
        "instrument": SYMBOL,
        "method": "linked_sequence",
        "verified": True,
        "snapshot": True,
        "sequence": 10,
        "previous_sequence": -1,
        "channel_depth": 400,
    }
    assert protocol.consume(message(symbol=SECOND_SYMBOL)).synchronized == {
        SYMBOL,
        SECOND_SYMBOL,
    }


def test_okx_quarantines_a_live_checksum_field_outside_the_current_protocol():
    protocol = OkxProtocol([SYMBOL], 25)
    protocol.consume(message())
    with pytest.raises(StreamInvalid, match="UNEXPECTED_CHECKSUM"):
        protocol.consume(message("update", previous=10, sequence=11, checksum=-123456))
    assert not protocol.consume("pong").synchronized










