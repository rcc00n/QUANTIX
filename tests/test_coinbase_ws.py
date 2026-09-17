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


def test_updates_are_exact_absolute_sizes_and_heartbeat_never_refreshes_quote():
    protocol = CoinbaseProtocol([PRODUCT, OTHER_PRODUCT], 25)
    first = send(protocol, heartbeat(sequence=90))
    assert first.activity and first.books == () and not first.synchronized
    # Coinbase may include an undocumented snapshot `time`. Keep it distinct
    # from a documented exchange engine update time and do not publish it.
    event = send(protocol, snapshot(time=NOW.isoformat()))
    symbol, book = event.books[0]
    assert symbol == PRODUCT and event.synchronized == {PRODUCT}
    assert book["timestamp"] is None
    assert book["timestamp_basis"] == "no_verified_snapshot_event_time"
    assert book["bids"][0] == [Decimal("100.00"), Decimal("1.000000000000000001")]
    assert book["asks"][0] == [Decimal(102), Decimal(3)]
    assert book["stream_integrity"]["update_sequence"] == "not_supplied"
    assert book["stream_integrity"]["checksum"] == "not_supplied"
    update = delta(
        [
            ["buy", "100.0000", "0"],
            ["buy", "99", "2.22"],
            ["buy", "99", "3.33"],
            ["sell", "102", "0.314159265358979323"],
        ]
    )
    result = send(protocol, update)
    raw = result.books[0][1]
    assert raw["timestamp"] == update["time"]
    assert raw["timestamp_basis"] == "exchange_batch_latest_event"
    assert raw["nonce"] is None
    assert raw["bids"] == [[Decimal(99), Decimal("3.33")], [Decimal(98), Decimal(2)]]
    assert raw["asks"][0][1] == Decimal("0.314159265358979323")
    # Product-level heartbeats can skip arbitrary sequence values between
    # heartbeats. Those values cannot demonstrate a gap in the batch channel.
    idle = send(protocol, heartbeat(sequence=2000))
    assert idle.activity and not idle.books and idle.synchronized == {PRODUCT}
    duplicate = send(protocol, update)
    assert not duplicate.activity and not duplicate.books
    # A different delivered message can share the same engine timestamp.
    same_timestamp = send(protocol, delta([["buy", "99", "4"]]))
    assert same_timestamp.books[0][1]["bids"][0] == [Decimal(99), Decimal(4)]
    other = send(protocol, snapshot(product_id=OTHER_PRODUCT))
    assert other.synchronized == {PRODUCT, OTHER_PRODUCT}


def test_top_levels_stay_exact_after_deletion_until_the_frontier_is_exhausted():
    protocol = CoinbaseProtocol([PRODUCT], 10)
    send(
        protocol,
        snapshot(
            bids=[[str(price), "1"] for price in range(100, 400)],
            asks=[["500", "1"], ["510", "1"]],
        ),
    )
    update = send(protocol, delta([["buy", "399", "0"], ["buy", "101", "0"]]))
    assert [price for price, _ in update.books[0][1]["bids"]] == list(range(398, 388, -1))
    # Prices below 144 still exist, but their intervening deltas were outside
    # the tracked frontier. Falling below ten proven bids requires a resync.
    with pytest.raises(StreamInvalid, match="DEPTH_FRONTIER_EXHAUSTED"):
        send(protocol, delta([["buy", str(p), "0"] for p in range(153, 399)], step=2))
    with pytest.raises(StreamInvalid, match="DELTA_WITHOUT_SNAPSHOT"):
        send(protocol, delta([["buy", "90", "5"]], step=3))


def test_bounded_frontier_matches_full_synthetic_order_book_oracle():
    protocol = CoinbaseProtocol([PRODUCT], 25)
    rng = random.Random(150)
    all_rows = {
        "buy": {price: Decimal(1) for price in range(100, 500)},
        "sell": {price: Decimal(1) for price in range(700, 1100)},
    }
    send(
        protocol,
        snapshot(
            bids=[[str(price), "1"] for price in all_rows["buy"]],
            asks=[[str(price), "1"] for price in all_rows["sell"]],
        ),
    )
    for step in range(1, 121):
        changes = []
        for _ in range(16):
            side = rng.choice(("buy", "sell"))
            start, end = (75, 550) if side == "buy" else (650, 1150)
            price = rng.randrange(start, end)
            quantity = Decimal(rng.randrange(3)) / Decimal(100)
            changes.append([side, str(price), str(quantity)])
            if quantity:
                all_rows[side][price] = quantity
            else:
                all_rows[side].pop(price, None)
        book = send(protocol, delta(changes, step)).books[0][1]
        for field, side in (("bids", "buy"), ("asks", "sell")):
            full = sorted(all_rows[side], reverse=side == "buy")[:25]
            assert book[field] == [[price, all_rows[side][price]] for price in full]
            assert len(getattr(protocol.books[PRODUCT], field).levels) <= 256


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"type": "error", "message": "SYNTHETIC exchange rejection"}, "SUBSCRIPTION_REJECTED"),
        (snapshot(product_id="SYNTHETIC-OTHER"), "UNKNOWN_STREAM_PRODUCT"),
        (delta([["buy", "100", "2"]]), "DELTA_WITHOUT_SNAPSHOT"),
        (snapshot(bids=[["100", "1"], ["100.00", "2"]]), "INVALID_SNAPSHOT_LEVEL"),
        (snapshot(bids=[[100, "1"]]), "INVALID_LEVEL_TYPE"),
        (snapshot(asks=[["100", "2"]]), "INVALID_RECONSTRUCTED_BOOK"),
        (snapshot(bids=[]), "INVALID_SNAPSHOT_DEPTH"),
        (heartbeat(sequence=True), "INVALID_HEARTBEAT_SEQUENCE"),
    ],
)
def test_malformed_and_unavailable_market_frames_fail_closed(payload, code):
    exchange = CoinbaseProtocol([PRODUCT], 25)
    with pytest.raises(StreamInvalid, match=code):
        send(exchange, payload)
    assert not exchange.books


def test_out_of_order_and_untimed_update_require_fresh_connection_snapshot():
    protocol = CoinbaseProtocol([PRODUCT], 25)
    send(protocol, snapshot())
    with pytest.raises(StreamInvalid, match="INVALID_EVENT_TIMESTAMP"):
        send(protocol, delta([["buy", "100", "7"]], time="2026-03-04T00:00:01"))
    assert not protocol.books
    send(protocol, snapshot())
    send(protocol, delta([["buy", "100", "7"]], step=2))
    with pytest.raises(StreamInvalid, match="OUT_OF_ORDER_BOOK_EVENT"):
        send(protocol, delta([["buy", "100", "3"]], step=1))
    with pytest.raises(StreamInvalid, match="DELTA_WITHOUT_SNAPSHOT"):
        send(protocol, delta([["buy", "100", "4"]], step=3))
    send(protocol, snapshot())
    with pytest.raises(StreamInvalid, match="UNEXPECTED_SECOND_SNAPSHOT"):
        send(protocol, snapshot())


