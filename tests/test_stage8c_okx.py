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


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        (message("update", previous=9, sequence=11), "SEQUENCE_GAP"),
        (message("update", previous=10, sequence=9), "SEQUENCE_RESET"),
        (message("update", previous=10, sequence=10), "CHANGED_DUPLICATE_SEQUENCE"),
        (
            message("update", previous=10, sequence=11, timestamp=str(int(TS) - 1)),
            "REGRESSED_BOOK_TIMESTAMP",
        ),
        (message("update", previous=10, sequence=True), "INVALID_SEQUENCE"),
        (
            message("update", previous=10, sequence=11, timestamp=None),
            "INVALID_BOOK_TIMESTAMP",
        ),
        (
            message("update", previous=10, sequence=11, timestamp=int(TS)),
            "INVALID_BOOK_TIMESTAMP",
        ),
        (
            message("update", previous=10, sequence=11, bids=[[98.1, "2", "0", "1"]]),
            "INVALID_BOOK_LEVEL",
        ),
        (
            message("update", previous=10, sequence=11, asks=[level(100, -1)]),
            "INVALID_BOOK_LEVEL",
        ),
        (
            message("update", previous=10, sequence=11, asks=[level(100, "Infinity")]),
            "INVALID_BOOK_LEVEL",
        ),
        (
            message(
                "update",
                previous=10,
                sequence=11,
                asks=[level(100, 1), level("100.00", 2)],
            ),
            "INVALID_BOOK_LEVEL",
        ),
        (
            message("update", previous=10, sequence=11, symbol="SOL-USDT"),
            "UNKNOWN_STREAM_SYMBOL",
        ),
        (message(previous=2), "INVALID_SNAPSHOT_SEQUENCE"),
        (message(sequence=11), "UNEXPECTED_SNAPSHOT"),
        (json.dumps({"event": "error", "code": "60018"}), "SUBSCRIPTION_REJECTED"),
        (json.dumps({"event": "notice", "code": "64008"}), "EXCHANGE_MAINTENANCE"),
        ("{", "INVALID_STREAM_MESSAGE"),
    ],
)
def test_okx_invalid_input_discards_state_until_new_snapshot(update, reason):
    protocol = OkxProtocol([SYMBOL], 25)
    protocol.consume(message())
    with pytest.raises(StreamInvalid, match=f"^{reason}$"):
        protocol.consume(update)
    assert not protocol.consume("pong").synchronized
    with pytest.raises(StreamInvalid, match="DELTA_WITHOUT_SNAPSHOT"):
        protocol.consume(message("update", previous=10, sequence=11))
    assert protocol.consume(message(sequence=20)).synchronized == {SYMBOL}


def test_okx_retains_full_depth_and_trims_book_to_published_channel_limit():
    protocol = OkxProtocol([SYMBOL], 400)
    protocol.consume(
        message(
            bids=[level(price, 1) for price in range(500, 100, -1)],
            asks=[level(600, 1)],
        )
    )
    added = protocol.consume(
        message("update", previous=10, sequence=11, bids=[level(510, 1)], asks=[])
    ).books[0][1]["bids"]
    assert len(added) == 400 and added[0][0] == 510 and added[-1][0] == 102
    removed = protocol.consume(
        message(
            "update",
            previous=11,
            sequence=12,
            bids=[level(510, 0), level(101, 1)],
            asks=[],
        )
    ).books[0][1]["bids"]
    assert len(removed) == 400 and removed[-1][0] == 101
    protocol = OkxProtocol([SYMBOL], 25)
    with pytest.raises(StreamInvalid, match="INVALID_BOOK_LEVELS"):
        protocol.consume(message(bids=[level(price, 1) for price in range(500, 99, -1)]))


@pytest.mark.parametrize("symbols", [[], ["BTC/USDT"], ["BTC-USDT-SWAP"], [SYMBOL, SYMBOL]])
def test_okx_requires_unique_native_spot_instruments(symbols):
    with pytest.raises(ValueError, match="unique native spot instrument IDs"):
        OkxProtocol(symbols, 25)
