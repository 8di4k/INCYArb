from incy_arb.flash_credit import INT128_MAX, format_units


def test_format_units_is_exact_and_does_not_use_float() -> None:
    assert format_units(1_230_407_108_381, 6) == "1230407.108381"
    assert format_units(508_000_000_000_000_000_000, 18) == "508"
    assert format_units(1, None) is None
    assert INT128_MAX == 170141183460469231731687303715884105727
