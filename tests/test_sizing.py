from incy_arb.sizing import INT128_MAX, optimize_size


def test_finds_smooth_interior_optimum() -> None:
    def quote(x: int) -> int:
        profit = 100_000 - (x - 400_000) ** 2 // 2_000_000
        return x + profit

    result = optimize_size(quote, 1_000, 1_000_000, 2_000_000)
    assert result is not None
    assert abs(result.amount_in - 400_000) < 2_000


def test_coarse_grid_then_refines_multiple_local_peaks() -> None:
    def quote(x: int) -> int:
        first = 40_000 - (x - 20_000) ** 2 // 20_000
        second = 90_000 - (x - 500_000) ** 2 // 500_000
        return max(0, x + max(first, second))

    result = optimize_size(quote, 1_000, 1_000_000, 1_000_000, grid_points=65)
    assert result is not None
    assert abs(result.amount_in - 500_000) < 5_000
    assert result.gross_profit >= 89_900


def test_infeasible_quotes_can_put_optimum_at_boundary() -> None:
    result = optimize_size(
        lambda x: None if x > 123_400 else x + x // 100,
        1_000, 1_000_000, 1_000_000, grid_points=65,
    )
    assert result is not None
    assert 120_000 <= result.amount_in <= 123_400


def test_flash_margin_and_int128_limit_clamp_search() -> None:
    result = optimize_size(lambda x: x + x // 100, 100, 10_000, 5_000, safety_margin=500)
    assert result is not None
    assert result.effective_max == 4_500
    assert result.limiting_cap == "flash_capacity"
    assert result.amount_in == 4_500

    huge = optimize_size(lambda x: x + 1, INT128_MAX - 1, 1 << 200, 1 << 200)
    assert huge is not None and huge.effective_max == INT128_MAX
    assert huge.limiting_cap == "int128_accounting"


def test_no_positive_quote_returns_none() -> None:
    assert optimize_size(lambda x: x - 1, 10, 1_000, 1_000) is None
    assert optimize_size(lambda _x: None, 10, 1_000, 1_000) is None


def test_negative_minimum_does_not_hide_profitable_larger_band() -> None:
    def quote(x: int) -> int:
        profit = 2_000 - abs(x - 50_000) // 10
        return max(0, x + profit)

    assert quote(1_000) < 1_000
    result = optimize_size(quote, 1_000, 100_000, 100_000)
    assert result is not None
    assert abs(result.amount_in - 50_000) < 1_000
