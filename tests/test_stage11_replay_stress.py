"""SYNTHETIC post-delay captures exercise the causal research gate."""

from datetime import UTC, datetime, timedelta

from arbiq.paper_trading.replay_stress import capture_window

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def book(side, received, *, observed=None, trusted=True, error_ms=100):
    when = NOW + timedelta(seconds=received)
    stamped = NOW + timedelta(seconds=received if observed is None else observed)
    return side, stamped.isoformat(), when.isoformat(), "HEALTHY", trusted, error_ms


def test_first_pair_must_follow_delay_and_each_capture_must_be_trusted():
    due, evaluated = NOW + timedelta(seconds=5), NOW + timedelta(seconds=8)
    assert capture_window([book("buy", 6), book("sell", 7)], due, evaluated) == (
        ["buy", "sell"],
        None,
    )
    for invalid_sell in (
        book("sell", 4),
        book("sell", 9),
        book("sell", 7, observed=10),
        book("sell", 7, trusted=False),
        book("sell", 7, error_ms=None),
    ):
        assert capture_window([book("buy", 6), invalid_sell], due, evaluated) == (
            ["buy"],
            "POST_DELAY_CAUSAL_BOOK_EVIDENCE_MISSING",
        )


def test_two_independently_observed_legs_can_fail_pair_synchronization():
    sides, reason = capture_window(
        [book("buy", 6, observed=4, error_ms=1500), book("sell", 14, error_ms=1500)],
        NOW + timedelta(seconds=5),
        NOW + timedelta(seconds=15),
    )
    assert sides == ["buy", "sell"]
    assert reason == "POST_DELAY_BOOK_SKEW_UNVERIFIED"
