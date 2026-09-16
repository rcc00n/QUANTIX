"""SYNTHETIC financial inputs only; these are not market observations."""

from decimal import Decimal, localcontext

import pytest
from arbiq.finance.contracts import exact_decimal
from arbiq.finance.money import convert_to_cad, net_simulated_pnl, round_quantity_down
from arbiq.market_data.contracts import BookLevel
from arbiq.settings.contracts import StrategySettings
from pydantic import ValidationError


@pytest.mark.parametrize(
    "value",
    [
        0.1,
        float("nan"),
        float("inf"),
        True,
        "NaN",
        "Infinity",
        "0.0000000000000000001",
        "100000000000000000000",
    ],
)
def test_reject_inexact_or_unstorable_money(value):
    with pytest.raises(ValueError):
        exact_decimal(value)


def test_decimal_precision_and_signed_conversion():
    with localcontext() as ctx:
        ctx.prec = 6  # Caller's decimal context cannot change a financial outcome.
        assert convert_to_cad(
            Decimal("123456789.123456789"), Decimal("1.3725")
        ) == Decimal("169444443.0719444429025")
        assert convert_to_cad(Decimal("-0.1"), Decimal("1.25")) == Decimal("-0.125")
    assert convert_to_cad(Decimal("0.1"), Decimal("0.2")) == Decimal("0.02")


@pytest.mark.parametrize(
    "quantity,step,expected",
    [
        ("1.239", "0.01", "1.23"),
        ("1.29", "0.05", "1.25"),
        ("0.000000019", "0.00000001", "0.00000001"),
        ("0", "0.1", "0.0"),
    ],
)
def test_exchange_quantity_step_rounds_down(quantity, step, expected):
    assert round_quantity_down(Decimal(quantity), Decimal(step)) == Decimal(expected)


def test_pnl_keeps_fees_and_expenses_separate_and_allows_losses():
    assert net_simulated_pnl(
        Decimal("10.01"), Decimal("2.02"), Decimal("1.03")
    ) == Decimal("6.96")
    assert net_simulated_pnl(Decimal(1), Decimal(2), Decimal(3)) == Decimal(-4)


@pytest.mark.parametrize(
    "operation,args",
    [
        (convert_to_cad, (Decimal(1), Decimal(0))),
        (convert_to_cad, (Decimal(1), Decimal(-1))),
        (round_quantity_down, (Decimal(-1), Decimal("0.1"))),
        (round_quantity_down, (Decimal(1), Decimal(0))),
        (net_simulated_pnl, (Decimal(1), Decimal(-1), Decimal(0))),
        (net_simulated_pnl, (Decimal(1), Decimal(0), Decimal(-1))),
    ],
)
def test_invalid_financial_inputs(operation, args):
    with pytest.raises(ValueError):
        operation(*args)


