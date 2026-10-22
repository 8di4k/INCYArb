"""Deterministic, read-only sizing for routes with piecewise quote curves."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Awaitable, Callable, Generator, TypedDict, Unpack


Quote = Callable[[int], int | None]
INT128_MAX = (1 << 127) - 1


class SizeOptions(TypedDict, total=False):
    risk_cap: int | None
    safety_margin: int
    grid_points: int
    refinement_iterations: int
    log_tolerance: float
    minimum_gross_profit: int


@dataclass(frozen=True)
class SizePoint:
    amount_in: int
    amount_out: int | None

    @property
    def gross_profit(self) -> int | None:
        return None if self.amount_out is None else self.amount_out - self.amount_in


@dataclass(frozen=True)
class SizeResult:
    amount_in: int
    amount_out: int
    gross_profit: int
    effective_max: int
    limiting_cap: str
    evaluations: tuple[SizePoint, ...]


def _log_grid(lo: int, hi: int, count: int) -> list[int]:
    if lo == hi:
        return [lo]
    a, b = math.log(lo), math.log(hi)
    return sorted({lo, hi, *(max(lo, min(hi, round(math.exp(a + (b - a) * i / (count - 1)))))
                             for i in range(count))})


def _size_search(
    minimum: int,
    market_cap: int,
    flash_capacity: int,
    *,
    risk_cap: int | None = None,
    safety_margin: int = 0,
    grid_points: int = 33,
    refinement_iterations: int = 32,
    log_tolerance: float = 1e-4,
    minimum_gross_profit: int = 0,
) -> Generator[int, int | None, SizeResult | None]:
    """Find the best gross PnL without assuming the full curve is unimodal.

    A coarse logarithmic grid locates every visible local peak. Each peak's
    neighbouring interval is then refined with golden section search. ``None``
    quotes are infeasible and score as negative infinity. The hard ceiling also
    includes PoolManager's signed-int128 accounting limit.
    """
    if minimum <= 0 or market_cap <= 0 or flash_capacity < 0 or safety_margin < 0:
        raise ValueError("sizes and caps must be positive; safety margin may be zero")
    if risk_cap is not None and risk_cap <= 0:
        raise ValueError("risk_cap must be positive")
    if grid_points < 3 or refinement_iterations < 0 or log_tolerance <= 0:
        raise ValueError("grid_points >= 3, nonnegative iterations, and positive tolerance are required")

    caps = {"market_cap": market_cap, "flash_capacity": max(0, flash_capacity - safety_margin),
            "int128_accounting": INT128_MAX}
    if risk_cap is not None:
        caps["risk_cap"] = risk_cap
    limiting_cap, effective_max = min(caps.items(), key=lambda item: item[1])
    if effective_max < minimum:
        return None

    cache: dict[int, int | None] = {}

    def evaluate(amount: int) -> Generator[int, int | None, int | float]:
        amount = max(minimum, min(effective_max, amount))
        if amount not in cache:
            output = yield amount
            if output is not None and (output < 0 or output > (1 << 256) - 1):
                raise ValueError("quote output must be uint256 or None")
            cache[amount] = output
        output = cache[amount]
        return -math.inf if output is None else output - amount

    grid = _log_grid(minimum, effective_max, grid_points)
    scores = []
    for x in grid:
        scores.append((yield from evaluate(x)))
    intervals: set[tuple[int, int]] = set()
    for i, score in enumerate(scores):
        left = scores[i - 1] if i else -math.inf
        right = scores[i + 1] if i + 1 < len(scores) else -math.inf
        if score >= left and score >= right and score > -math.inf:
            intervals.add((grid[max(0, i - 1)], grid[min(len(grid) - 1, i + 1)]))

    golden = (math.sqrt(5) - 1) / 2
    for interval_lo, interval_hi in sorted(intervals):
        if interval_lo == interval_hi:
            continue
        a, b = math.log(interval_lo), math.log(interval_hi)
        c, d = b - golden * (b - a), a + golden * (b - a)
        fc = yield from evaluate(round(math.exp(c)))
        fd = yield from evaluate(round(math.exp(d)))
        for _ in range(refinement_iterations):
            if b - a <= log_tolerance:
                break
            if fc >= fd:
                b, d, fd = d, c, fc
                c = b - golden * (b - a)
                fc = yield from evaluate(round(math.exp(c)))
            else:
                a, c, fc = c, d, fd
                d = a + golden * (b - a)
                fd = yield from evaluate(round(math.exp(d)))
        yield from evaluate(round(math.exp((a + b) / 2)))

    feasible = [(output - amount, amount, output) for amount, output in cache.items() if output is not None]
    if not feasible:
        return None
    profit, amount, output = max(feasible)
    if profit <= minimum_gross_profit:
        return None
    points = tuple(SizePoint(x, cache[x]) for x in sorted(cache))
    return SizeResult(amount, output, profit, effective_max, limiting_cap, points)


def optimize_size(
    quote: Quote, minimum: int, market_cap: int, flash_capacity: int, **options: Unpack[SizeOptions],
) -> SizeResult | None:
    """Size a synchronous quote function using the shared cached search."""
    search = _size_search(minimum, market_cap, flash_capacity, **options)
    output = None
    while True:
        try:
            amount = search.send(output)
        except StopIteration as done:
            return done.value
        output = quote(amount)


async def optimize_size_async(
    quote: Callable[[int], Awaitable[int | None]],
    minimum: int, market_cap: int, flash_capacity: int, **options: Unpack[SizeOptions],
) -> SizeResult | None:
    """Use the same search with sequential, awaited on-chain quotes."""
    search = _size_search(minimum, market_cap, flash_capacity, **options)
    output = None
    while True:
        try:
            amount = search.send(output)
        except StopIteration as done:
            return done.value
        output = await quote(amount)
