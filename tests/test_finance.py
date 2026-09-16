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
