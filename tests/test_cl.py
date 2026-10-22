import asyncio
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from incy_arb import cl_snapshot
from incy_arb.abi import function_selector
from incy_arb.cl_math import MAX_SQRT, MIN_SQRT, Q96, U256, next_sqrt_from_input, sqrt_at_tick, swap_exact_in
from incy_arb.cl_snapshot import BitmapTickMap, CLSnapshot, check_cl_parity, load_cl_snapshot
from incy_arb.rpc import RpcError

A = "0x" + "11" * 20
B = "0x" + "22" * 20
P = "0x" + "33" * 20
ZERO = "0x" + "00" * 20


def pool(venue="slipstream"):
    result = {"address": P, "token0": A, "token1": B, "tick_spacing": 60, "fee_pips": 3000, "hooks": ZERO}
    if venue == "uniswap_v4":
        result["address"] = (
            "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [(A, B, 3000, 60, ZERO)])).hex()
        )
    return result


class SnapshotRpc:
    def __init__(self):
        self.blocks = []
        self.reads = 0
        self.reorg = False
        self.fail_bitmap = False

    async def chain_id(self):
        return 57073

    async def sealed_block(self, block):
        self.blocks.append(block)
        self.reads += 1
        return {"hash": "0xbb" if self.reorg and self.reads > 1 else "0xaa"}

    async def multicall(self, calls, block):
        self.blocks.append(block)
        result = []
        for call in calls:
            selector = call.data[:4]
            if selector == function_selector("slot0()"):
                raw = encode(["uint160", "int24"], [Q96, 0])
            elif selector == function_selector("getSlot0(bytes32)"):
                raw = encode(["uint160", "int24", "uint24", "uint24"], [Q96, 0, 0, 3000])
            elif selector in (function_selector("liquidity()"), function_selector("getLiquidity(bytes32)")):
                raw = encode(["uint128"], [10**18])
            elif selector == function_selector("fee()"):
                raw = encode(["uint24"], [3000])
            elif selector == function_selector("tickSpacing()"):
                raw = encode(["int24"], [60])
            elif selector == function_selector("token0()"):
                raw = encode(["address"], [A])
            elif selector == function_selector("token1()"):
                raw = encode(["address"], [B])
            elif selector in (
                function_selector("tickBitmap(int16)"),
                function_selector("getTickBitmap(bytes32,int16)"),
            ):
                if self.fail_bitmap:
                    result.append((False, b""))
                    continue
                word = decode(["int16"], call.data[-32:])[0]
                bitmap = 1 << 255 if word == -1 else 1 << 1 if word == 0 else 0
                raw = encode(["uint256"], [bitmap])
            else:
                tick = decode(["int24"], call.data[-32:])[0]
                raw = encode(["uint128", "int128"], [10**18, 10**18 if tick < 0 else -(10**18)])
            result.append((True, raw))
        return result


def test_tickmath_known_core_vectors():
    assert sqrt_at_tick(0) == Q96
    assert sqrt_at_tick(-887272) == MIN_SQRT
    assert sqrt_at_tick(887272) == MAX_SQRT
    with pytest.raises(ValueError):
        sqrt_at_tick(887273)


def test_bitmap_iteration_keeps_uninitialized_word_boundaries_and_negative_flooring():
    ticks = BitmapTickMap({-300: 1, 300: -1}, -512, 511, 1)
    assert ticks.next_below_or_at(10) == 0
    assert ticks.next_below_or_at(-1) == -256
    assert ticks.next_below_or_at(-257) == -300
    assert ticks.next_above(-2) == -1
    assert ticks.next_above(-1) == 255
    assert ticks.next_above(255) == 300


def test_loaded_tick_window_is_not_extrapolated():
    ticks = BitmapTickMap({}, -256, 255, 1)
    assert swap_exact_in(Q96, 0, 10**18, 3000, ticks, True, 10**6)[0] == 996999
    assert swap_exact_in(Q96, 0, 10**18, 3000, ticks, True, 10**24) is None
    assert swap_exact_in(Q96, 0, 10**18, 1_000_000, ticks, True, 10**6) is None


def test_token0_overflow_path_keeps_solidity_division_order():
    amount = (1 << 127) - 1
    liquidity = 1 << 127
    price = MAX_SQRT - 1
    assert amount * price > U256
    numerator = liquidity << 96
    divisor = numerator // price + amount
    assert next_sqrt_from_input(price, liquidity, amount, True) == (numerator + divisor - 1) // divisor


@pytest.mark.parametrize("venue", ["slipstream", "uniswap_v4"])
def test_loader_reads_signed_tick_liquidity_and_pins_every_call(venue):
    rpc = SnapshotRpc()
    snapshot = asyncio.run(load_cl_snapshot(rpc, venue, pool(venue), 123, 1))
    assert snapshot.ticks.net == {-60: 10**18, 60: -(10**18)}
    assert snapshot.liquidity_gross == {-60: 10**18, 60: 10**18}
    assert snapshot.quote(True, 10**6) == 996999
    assert snapshot.quote(False, 10**6) == 996999
    assert set(rpc.blocks) == {123}


def test_packed_protocol_fee_is_directional_and_combined_with_lp_fee():
    snapshot = asyncio.run(load_cl_snapshot(SnapshotRpc(), "uniswap_v4", pool("uniswap_v4"), 123, 1))
    from dataclasses import replace

    snapshot = replace(snapshot, protocol_fee=100 | (500 << 12))
    assert snapshot.fee(True) == 3100
    assert snapshot.fee(False) == 3499


@pytest.mark.parametrize("failure", ["bitmap", "reorg"])
def test_missing_tick_word_or_replaced_block_never_produces_snapshot(failure):
    rpc = SnapshotRpc()
    if failure == "bitmap":
        rpc.fail_bitmap = True
    else:
        rpc.reorg = True
    with pytest.raises(RuntimeError):
        asyncio.run(load_cl_snapshot(rpc, "slipstream", pool(), 123, 1))


def test_parity_distinguishes_exact_match_difference_and_unmodeled(monkeypatch):
    async def quote(*args):
        return {"amount_out": 996999}

    monkeypatch.setattr(cl_snapshot, "_quote", quote)
    result = asyncio.run(check_cl_parity(SnapshotRpc(), "slipstream", pool(), 123, (10**6,), (10**6,), 1))
    assert result["summary"]["match"] == 2 and result["summary"]["difference"] == 0

    async def different(*args):
        return {"amount_out": 997000}

    monkeypatch.setattr(cl_snapshot, "_quote", different)
    result = asyncio.run(check_cl_parity(SnapshotRpc(), "slipstream", pool(), 123, (10**6,), (10**6,), 1))
    assert result["summary"]["difference"] == 2
    result = asyncio.run(check_cl_parity(SnapshotRpc(), "slipstream", pool(), 123, (10**24,), (10**24,), 1))
    assert result["summary"]["not_fully_modeled"] == 2


def test_reverting_quoter_is_not_claimed_as_a_match(monkeypatch):
    async def failed(*args):
        raise RpcError("reverted", code=3)

    monkeypatch.setattr(cl_snapshot, "_quote", failed)
    result = asyncio.run(check_cl_parity(SnapshotRpc(), "slipstream", pool(), 123, (10**6,), (10**6,), 1))
    assert result["summary"]["quoter_error"] == 2 and result["summary"]["match"] == 0


def test_local_model_replays_130_independent_on_chain_quote_vectors():
    path = Path(__file__).resolve().parents[1] / "docs/validation/cl-parity-59198585.json"
    report = json.loads(path.read_text())
    checked = 0
    for result in report["results"]:
        record = result["snapshot"]
        window = record["tick_window"]
        ticks = BitmapTickMap(
            {int(t): int(n) for t, n in window["liquidity_net"].items()},
            window["lo"],
            window["hi"],
            record["tick_spacing"],
        )
        snapshot = CLSnapshot(
            record["venue"],
            record["pool"],
            record["block_number"],
            record["block_hash"],
            int(record["sqrt_price_x96"]),
            record["tick"],
            int(record["liquidity"]),
            record["tick_spacing"],
            record["lp_fee"],
            record["protocol_fee"],
            ticks,
        )
        for point in result["points"]:
            if point["status"] == "match":
                assert snapshot.quote(point["zero_for_one"], int(point["amount_in"])) == int(point["quoter_amount_out"])
                checked += 1
    assert checked == 130


def test_native_v4_currency_uses_stateview_without_an_erc20_zero_call():
    value = pool("uniswap_v4")
    value["token0"] = ZERO
    value["token1"] = A
    value["address"] = (
        "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [(ZERO, A, 3000, 60, ZERO)])).hex()
    )
    snapshot = asyncio.run(load_cl_snapshot(SnapshotRpc(), "uniswap_v4", value, 123, 1))
    assert snapshot.pool["token0"] == ZERO and snapshot.quote(True, 10**6) == 996999
