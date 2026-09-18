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


def test_okx_linked_delta_removes_levels_past_publication_depth_and_ignores_duplicate():
    protocol = OkxProtocol([SYMBOL], 2)
    snapshot = message(
        bids=[level(98, 2), level(99, 1), level(97, 3)],
        asks=[level(102, 3), level(100, 1), level(101, 2)],
    )
    first = protocol.consume(snapshot).books[0][1]
    assert [price for price, _ in first["bids"]] == [99, 98]
    assert [price for price, _ in first["asks"]] == [100, 101]
    assert not protocol.consume(snapshot).books

    delta = message(
        "update",
        previous=10,
        sequence=18,
        bids=[level(99, 0), level(98, 9)],
        asks=[level(100, 0)],
    )
    new = protocol.consume(delta).books[0][1]
    assert new["bids"] == [[Decimal(98), Decimal(9)], [Decimal(97), Decimal(3)]]
    assert new["asks"] == [[Decimal(101), Decimal(2)], [Decimal(102), Decimal(3)]]
    assert new["stream_integrity"]["previous_sequence"] == 10
    assert new["stream_integrity"]["sequence"] == 18
    assert not new["stream_integrity"]["snapshot"]
    assert [price for price, _ in first["bids"]] == [99, 98]
    repeat = protocol.consume(delta)
    assert repeat.synchronized == {SYMBOL} and not repeat.books


def test_okx_heartbeat_cannot_refresh_old_quotes_or_invalidate_link():
    protocol = OkxProtocol([SYMBOL], 25)
    assert protocol.consume("pong").pong
    protocol.consume(message())
    keepalive = protocol.consume(
        message(
            "update",
            previous=10,
            sequence=10,
            timestamp=str(int(TS) + 60000),
            bids=[],
            asks=[],
        )
    )
    assert keepalive.activity and not keepalive.pong and not keepalive.books
    assert keepalive.synchronized == {SYMBOL}
    pongs = protocol.consume(b"pong")
    assert pongs.synchronized == {SYMBOL} and pongs.pong and not pongs.books
    updated = protocol.consume(message("update", previous=10, sequence=20))
    assert updated.books[0][1]["timestamp"] == int(TS)






