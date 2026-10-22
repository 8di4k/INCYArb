"""Pinned CL tick windows and independent on-chain parity checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_abi import encode
from eth_utils import keccak

from .abi import ReadCall, decode_result, encode_call
from .cl_math import MAX_TICK, MIN_TICK, TickMap, swap_exact_in
from .research import _quote, _quote_v4
from .rpc import ReadOnlyRpc, RpcError
from .v4_state import V4_STATE_VIEW, decode_slot0


@dataclass(frozen=True)
class CLSnapshot:
    venue: str
    pool: dict[str, Any]
    block: int
    block_hash: str
    sqrt_price_x96: int
    tick: int
    liquidity: int
    tick_spacing: int
    lp_fee: int
    protocol_fee: int
    ticks: TickMap
    liquidity_gross: dict[int, int] = field(default_factory=dict)

    def fee(self, zero_for_one: bool) -> int:
        protocol = (self.protocol_fee & 0xFFF) if zero_for_one else self.protocol_fee >> 12
        return protocol + self.lp_fee - protocol * self.lp_fee // 1_000_000

    def quote(self, zero_for_one: bool, amount: int) -> int | None:
        if (amount <= 0 or not 0 <= self.fee(zero_for_one) < 1_000_000
                or not self.ticks.lo <= self.tick <= self.ticks.hi):
            return None
        result = swap_exact_in(
            self.sqrt_price_x96, self.tick, self.liquidity, self.fee(zero_for_one), self.ticks, zero_for_one, amount
        )
        return result[0] if result is not None else None

    def record(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "pool": self.pool,
            "block_number": self.block,
            "block_hash": self.block_hash,
            "sqrt_price_x96": str(self.sqrt_price_x96),
            "tick": self.tick,
            "liquidity": str(self.liquidity),
            "tick_spacing": self.tick_spacing,
            "lp_fee": self.lp_fee,
            "protocol_fee": self.protocol_fee,
            "tick_window": {
                "lo": self.ticks.lo,
                "hi": self.ticks.hi,
                "liquidity_net": {str(t): str(n) for t, n in self.ticks.net.items()},
                "liquidity_gross": {str(t): str(n) for t, n in self.liquidity_gross.items()},
            },
        }


async def load_cl_snapshot(
    rpc: ReadOnlyRpc, venue: str, pool: dict[str, Any], block: int, words_around: int = 3,
    *, window_tick: int | None = None,
) -> CLSnapshot:
    if venue not in ("slipstream", "uniswap_v4") or not 0 <= words_around <= 16:
        raise ValueError("CL snapshot requires Slipstream/static zero-hook v4 and 0–16 surrounding words")
    if await rpc.chain_id() != 57073:
        raise ValueError("CL snapshot requires Ink RPC")
    before = await rpc.sealed_block(block)
    if not before or not before.get("hash"):
        raise RuntimeError("cannot pin CL block hash")
    identity = pool["address"].lower()
    if venue == "uniswap_v4":
        fee, spacing = int(pool["fee_pips"]), int(pool["tick_spacing"])
        if int(pool["hooks"], 16) != 0 or fee & 0x800000 or not 0 < spacing < 1 << 23:
            raise ValueError("hooks/dynamic fee or invalid tick spacing are unsupported")
        a, b = pool["token0"].lower(), pool["token1"].lower()
        if a >= b:
            raise ValueError("v4 snapshot requires ordered distinct currencies")
        key = (a, b, fee, spacing, pool["hooks"])
        if identity != "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [key])).hex():
            raise ValueError("pool ID differs from its key")
        pid = bytes.fromhex(identity[2:])
        head_calls = [
            ReadCall(V4_STATE_VIEW, encode_call("getSlot0(bytes32)", ["bytes32"], [pid]), "slot0"),
            ReadCall(V4_STATE_VIEW, encode_call("getLiquidity(bytes32)", ["bytes32"], [pid]), "liquidity"),
        ]
    else:
        head_calls = [
            ReadCall(identity, encode_call(sig), sig)
            for sig in ("slot0()", "liquidity()", "fee()", "tickSpacing()", "token0()", "token1()")
        ]
    replies = await rpc.multicall(head_calls, block)
    if any(not success for success, data in replies):
        raise RuntimeError("CL metadata call failed")
    raw = [data for success, data in replies]
    if venue == "uniswap_v4":
        slot = decode_slot0(raw[0])
        sqrt, tick, lp, protocol = slot["sqrt_price_x96"], slot["tick"], slot["lp_fee"], slot["protocol_fee"]
    else:
        sqrt, tick = decode_result(["uint160", "int24"], raw[0][:64])
        lp = decode_result(["uint24"], raw[2])[0]
        protocol = 0
        spacing = decode_result(["int24"], raw[3])[0]
        a, b = (decode_result(["address"], data)[0].lower() for data in raw[4:6])
        if (a, b) != (pool["token0"].lower(), pool["token1"].lower()):
            raise ValueError("pool currencies differ from definition")
    liquidity = decode_result(["uint128"], raw[1])[0]
    if not 0 < spacing < 1 << 23 or not MIN_TICK <= tick <= MAX_TICK or not 0 <= lp <= 1_000_000:
        raise RuntimeError("invalid CL metadata")
    if window_tick is not None and not MIN_TICK <= window_tick <= MAX_TICK:
        raise ValueError("invalid fixed window tick")
    middle = ((tick if window_tick is None else window_tick) // spacing) >> 8
    words = list(range(middle - words_around, middle + words_around + 1))
    if venue == "uniswap_v4":
        calls = [
            ReadCall(V4_STATE_VIEW, encode_call("getTickBitmap(bytes32,int16)", ["bytes32", "int16"], [pid, w]), str(w))
            for w in words
        ]
    else:
        calls = [ReadCall(identity, encode_call("tickBitmap(int16)", ["int16"], [w]), str(w)) for w in words]
    bitmaps = await rpc.multicall(calls, block)
    if any(not ok for ok, data in bitmaps):
        raise RuntimeError("incomplete tick bitmap window")
    initialized = []
    for word, (ok, data) in zip(words, bitmaps, strict=True):
        bitmap = decode_result(["uint256"], data)[0]
        while bitmap:
            bit = (bitmap & -bitmap).bit_length() - 1
            initialized.append(((word << 8) + bit) * spacing)
            bitmap &= bitmap - 1
    net = {}
    gross_ticks = {}
    for offset in range(0, len(initialized), 40):
        chunk = initialized[offset : offset + 40]
        if venue == "uniswap_v4":
            calls = [
                ReadCall(
                    V4_STATE_VIEW,
                    encode_call("getTickLiquidity(bytes32,int24)", ["bytes32", "int24"], [pid, t]),
                    str(t),
                )
                for t in chunk
            ]
        else:
            calls = [ReadCall(identity, encode_call("ticks(int24)", ["int24"], [t]), str(t)) for t in chunk]
        ticks = await rpc.multicall(calls, block)
        if any(not ok for ok, data in ticks):
            raise RuntimeError("missing initialized tick liquidity")
        for t, (ok, data) in zip(chunk, ticks, strict=True):
            gross, delta = decode_result(["uint128", "int128"], data[:64])
            if gross <= 0 or not MIN_TICK <= t <= MAX_TICK:
                raise RuntimeError("invalid initialized tick")
            net[t] = delta
            gross_ticks[t] = gross
    after = await rpc.sealed_block(block)
    if not after or after.get("hash") != before["hash"]:
        raise RuntimeError("CL block replaced during snapshot")
    lo = max(MIN_TICK, (words[0] << 8) * spacing)
    hi = min(MAX_TICK, ((words[-1] + 1) << 8) * spacing - 1)
    return CLSnapshot(
        venue,
        pool,
        block,
        before["hash"],
        sqrt,
        tick,
        liquidity,
        spacing,
        lp,
        protocol,
        BitmapTickMap(net, lo, hi, spacing),
        liquidity_gross=gross_ticks,
    )


async def check_cl_parity(
    rpc: ReadOnlyRpc,
    venue: str,
    pool: dict[str, Any],
    block: int,
    amounts0: tuple[int, ...],
    amounts1: tuple[int, ...],
    words_around: int = 3,
) -> dict[str, Any]:
    if not amounts0 or not amounts1 or any(amount <= 0 or amount >= (1 << 127) for amount in amounts0 + amounts1):
        raise ValueError("positive signed-int128 amounts for both currencies are required")
    snapshot = await load_cl_snapshot(rpc, venue, pool, block, words_around)
    points = []
    for zero_for_one, amounts in ((True, amounts0), (False, amounts1)):
        token_in, token_out = (pool["token0"], pool["token1"]) if zero_for_one else (pool["token1"], pool["token0"])
        for amount in amounts:
            local = snapshot.quote(zero_for_one, amount)
            point = {
                "zero_for_one": zero_for_one,
                "amount_in": str(amount),
                "local_amount_out": str(local) if local is not None else None,
            }
            try:
                if venue == "uniswap_v4":
                    actual, gas = await _quote_v4(rpc, block, pool, token_in.lower(), amount)
                else:
                    actual = (await _quote(rpc, block, token_in, token_out, amount, snapshot.tick_spacing))[
                        "amount_out"
                    ]
                point["quoter_amount_out"] = str(actual)
                point["status"] = "not_fully_modeled" if local is None else "match" if local == actual else "difference"
            except RpcError as error:
                point.update(status="quoter_error", error=str(error), rpc_code=error.code)
            points.append(point)
    after = await rpc.sealed_block(block)
    if not after or after.get("hash") != snapshot.block_hash:
        raise RuntimeError("CL block replaced during parity check")
    return {
        "chain_id": 57073,
        "snapshot": snapshot.record(),
        "points": points,
        "summary": {
            status: sum(p["status"] == status for p in points)
            for status in ("match", "difference", "not_fully_modeled", "quoter_error")
        },
        "warning": "Parity only for sampled full fills within the loaded tick window. Partial fills, exhausted liquidity, and unsupported fees are not extrapolated. No live signal or execution permission.",
    }


class BitmapTickMap(TickMap):
    """Retain empty bitmap-word boundaries: SwapMath rounding happens at each step."""

    __slots__ = ("spacing",)

    def __init__(self, net: dict[int, int], lo: int, hi: int, spacing: int):
        super().__init__(net, lo, hi)
        self.spacing = spacing

    def next_below_or_at(self, tick: int):
        boundary = ((tick // self.spacing) >> 8) * 256 * self.spacing
        initialized = super().next_below_or_at(tick)
        return boundary if initialized is None else max(boundary, initialized)

    def next_above(self, tick: int):
        compressed = tick // self.spacing + 1
        boundary = (((compressed >> 8) + 1) * 256 - 1) * self.spacing
        initialized = super().next_above(tick)
        return boundary if initialized is None else min(boundary, initialized)


async def check_scan_cl_parity(
    scan: dict[str, Any],
    rpc_url: str,
    finding_index: int,
    step_index: int,
    amounts0: tuple[int, ...],
    amounts1: tuple[int, ...],
    words_around: int = 3,
) -> dict[str, Any]:
    if scan.get("chain_id") != 57073 or not 0 <= finding_index < len(scan["findings"]):
        raise ValueError("Ink scan and valid finding index required")
    steps = scan["findings"][finding_index]["simulation_route"]["steps"]
    if not 0 <= step_index < len(steps):
        raise ValueError("step index outside route")
    step = steps[step_index]
    async with ReadOnlyRpc(rpc_url) as rpc:
        result = await check_cl_parity(
            rpc, step["venue"], step["pool"], int(scan["quote_block_number"]), amounts0, amounts1, words_around
        )
    if scan.get("quote_block_hash") and scan["quote_block_hash"] != result["snapshot"]["block_hash"]:
        raise RuntimeError("scan block was replaced; rescan before checking parity")
    return result
