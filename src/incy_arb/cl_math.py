"""Точная целочисленная математика Uniswap v3 / v4 (только exactIn).

Ported from 8di4k/Arbitr bot/xarb_bot/v3math.py at 7b3ee78bed6d3620f00163b73dbbbe7bcc26ee69.
TickMath, SqrtPriceMath and SwapMath arithmetic for read-only CL parity checks.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

Q96 = 1 << 96
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT = 4295128739
MAX_SQRT = 1461446703485210103287273052203988822378723970342
U256 = (1 << 256) - 1


_SQRT_CACHE: dict[int, int] = {}
_SQRT_CACHE_MAX = 1 << 18


def sqrt_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick. Тики пулов повторяются от котировки к котировке, поэтому значения кэшируются:
    на поиске это была самая частая функция (~600 тыс. вызовов за 9 мин потока)."""
    r = _SQRT_CACHE.get(tick)
    if r is None:
        if len(_SQRT_CACHE) >= _SQRT_CACHE_MAX:
            _SQRT_CACHE.clear()
        r = _SQRT_CACHE[tick] = _sqrt_at_tick(tick)
    return r


def _sqrt_at_tick(tick: int) -> int:
    a = -tick if tick < 0 else tick
    if a > MAX_TICK:
        raise ValueError("tick")
    r = 0xFFFCB933BD6FAD37AA2D162D1A594001 if a & 0x1 else 0x100000000000000000000000000000000
    for bit, m in (
        (0x2, 0xFFF97272373D413259A46990580E213A),
        (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
        (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
        (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
        (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
        (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
        (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
        (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
        (0x200, 0xF987A7253AC413176F2B074CF7815E54),
        (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
        (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
        (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
        (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
        (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
        (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
        (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
        (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
        (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
        (0x80000, 0x48A170391F7DC42444E8FA2),
    ):
        if a & bit:
            r = (r * m) >> 128
    if tick > 0:
        r = U256 // r
    return (r >> 32) + (1 if r % (1 << 32) else 0)


def _div_up(a: int, b: int) -> int:
    return -(-a // b)


def amount0_delta(sa: int, sb: int, liq: int, up: bool) -> int:
    if sa > sb:
        sa, sb = sb, sa
    n1 = liq << 96
    n2 = sb - sa
    if up:
        return _div_up(_div_up(n1 * n2, sb), sa)
    return (n1 * n2 // sb) // sa


def amount1_delta(sa: int, sb: int, liq: int, up: bool) -> int:
    if sa > sb:
        sa, sb = sb, sa
    if up:
        return _div_up(liq * (sb - sa), Q96)
    return liq * (sb - sa) // Q96


def next_sqrt_from_input(s: int, liq: int, amount: int, zero_for_one: bool) -> int:
    if zero_for_one:
        # getNextSqrtPriceFromAmount0RoundingUp(add=true)
        if amount == 0:
            return s
        n1 = liq << 96
        product = amount * s
        denom = n1 + product
        if product <= U256 and denom <= U256:
            return _div_up(n1 * s, denom)
        # Solidity's product/denominator overflow path uses a different division order.
        return _div_up(n1, n1 // s + amount)
    # getNextSqrtPriceFromAmount1RoundingDown(add=true)
    return s + (amount << 96) // liq


def swap_step(s: int, target: int, liq: int, remaining: int, fee_pips: int):
    """computeSwapStep для exactIn. Возвращает (новая цена, вход без комиссии, выход, комиссия)."""
    zfo = s >= target
    rem_less_fee = remaining * (1_000_000 - fee_pips) // 1_000_000
    amount_in = amount0_delta(target, s, liq, True) if zfo else amount1_delta(s, target, liq, True)
    if rem_less_fee >= amount_in:
        nxt = target
    else:
        nxt = next_sqrt_from_input(s, liq, rem_less_fee, zfo)
    reached = nxt == target
    if zfo:
        if not reached:
            amount_in = amount0_delta(nxt, s, liq, True)
        amount_out = amount1_delta(nxt, s, liq, False)
    else:
        if not reached:
            amount_in = amount1_delta(s, nxt, liq, True)
        amount_out = amount0_delta(s, nxt, liq, False)
    if not reached:
        fee = remaining - amount_in
    else:
        fee = _div_up(amount_in * fee_pips, 1_000_000 - fee_pips)
    return nxt, amount_in, amount_out, fee


class TickMap:
    """Инициализированные тики пула в загруженном диапазоне [lo, hi]."""

    __slots__ = ("ticks", "net", "lo", "hi")

    def __init__(self, net: dict[int, int] | None = None, lo: int = MIN_TICK, hi: int = MAX_TICK):
        self.net: dict[int, int] = dict(net or {})
        self.ticks: list[int] = sorted(self.net)
        self.lo, self.hi = lo, hi

    def add(self, tick: int, delta: int) -> None:
        if tick not in self.net:
            self.net[tick] = 0
            i = bisect_left(self.ticks, tick)
            self.ticks.insert(i, tick)
        self.net[tick] += delta

    def next_below_or_at(self, tick: int):
        i = bisect_right(self.ticks, tick) - 1
        return self.ticks[i] if i >= 0 else None

    def next_above(self, tick: int):
        i = bisect_right(self.ticks, tick)
        return self.ticks[i] if i < len(self.ticks) else None


def swap_exact_in(
    sqrt_p: int,
    tick: int,
    liq: int,
    fee_pips: int,
    ticks: TickMap,
    zero_for_one: bool,
    amount_in: int,
    want_tick: bool = False,
):
    """Симуляция swap exactIn. Возвращает (amount_out, новая цена, новый тик, новая ликвидность)
    или None, если объём выходит за загруженный диапазон тиков (считаем, что так нельзя).
    Новый тик внутри диапазона считается только с want_tick (котировке он не нужен, а стоит логарифма и
    нескольких sqrt_at_tick); без него на этом месте None."""
    if amount_in <= 0 or not 0 <= liq < 1 << 128 or not 0 <= fee_pips < 1_000_000 or not MIN_SQRT <= sqrt_p < MAX_SQRT:
        return None
    remaining = amount_in
    out = 0
    s, t, L = sqrt_p, tick, liq
    while remaining > 0:
        if zero_for_one:
            nt = ticks.next_below_or_at(t)
            if nt is None or nt < ticks.lo:
                nt_bound = max(ticks.lo, MIN_TICK)
                target = sqrt_at_tick(nt_bound)
                known = False
            else:
                target = sqrt_at_tick(nt)
                known = True
        else:
            nt = ticks.next_above(t)
            if nt is None or nt > ticks.hi:
                nt_bound = min(ticks.hi, MAX_TICK)
                target = sqrt_at_tick(nt_bound)
                known = False
            else:
                target = sqrt_at_tick(nt)
                known = True
        if L == 0:
            if not known:
                return None
            s = target
        else:
            if (zero_for_one and target >= s) or (not zero_for_one and target <= s):
                if not known:
                    return None
            else:
                s_new, a_in, a_out, fee = swap_step(s, target, L, remaining, fee_pips)
                remaining -= a_in + fee
                out += a_out
                s = s_new
                if s != target:
                    t = tick_at_sqrt_approx(s) if want_tick else None
                    break
                if not known:
                    return None
        # пересекли инициализированный тик
        net = ticks.net.get(nt, 0)
        if zero_for_one:
            L -= net
            t = nt - 1
        else:
            L += net
            t = nt
        if not 0 <= L < 1 << 128:
            return None
    return out, s, t, L


def tick_at_sqrt_approx(s: int) -> int:
    """Тик для цены (через float; точность достаточна для поиска следующего тика,
    затем уточняем по sqrt_at_tick)."""
    t = math.floor(math.log((s / Q96) ** 2, 1.0001))
    while t < MAX_TICK and sqrt_at_tick(t + 1) <= s:
        t += 1
    while t > MIN_TICK and sqrt_at_tick(t) > s:
        t -= 1
    return t
