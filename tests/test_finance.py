def test_decimal_precision_and_signed_conversion():
    with localcontext() as ctx:
        ctx.prec = 6  # Caller's decimal context cannot change a financial outcome.
        assert convert_to_cad(
            Decimal("123456789.123456789"), Decimal("1.3725")
        ) == Decimal("169444443.0719444429025")
        assert convert_to_cad(Decimal("-0.1"), Decimal("1.25")) == Decimal("-0.125")
    assert convert_to_cad(Decimal("0.1"), Decimal("0.2")) == Decimal("0.02")
