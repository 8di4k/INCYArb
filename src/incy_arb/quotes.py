"""Deterministic, read-only quotes for the Ink Velodrome V2 constant-product path."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .abi import ReadCall, decode_result, encode_call
from .inventory import SLIPSTREAM_FACTORY, VELODROME_V2_FACTORY
from .observer import INK_RPC
from .rpc import ReadOnlyRpc


@dataclass(frozen=True)
class V2QuoteRequest:
    universe: Path
    pool: str
    token_in: str
    amount_in: int


def constant_product_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int | None:
    """Quote an exact-input V2 swap using the XArb2 volatile-pair equation.

    The formula assumes ``amount_in`` reaches the pair unchanged.  Fee-on-transfer
    tokens and stable-pool invariant pairs therefore have no quote support here.
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0 or not 0 <= fee_bps < 10_000:
        return None
    amount_after_fee = amount_in * (10_000 - fee_bps)
    amount_out = amount_after_fee * reserve_out // (reserve_in * 10_000 + amount_after_fee)
    return amount_out or None


def quote_v2_snapshot(request: V2QuoteRequest) -> dict[str, Any]:
    """Quote one explicitly admitted volatile V2 pool at its inventory snapshot."""
    if request.amount_in <= 0:
        raise ValueError("amount_in must be positive")
    universe, pool = _admitted_v2_pool(request)
    return _quote_v2_pool(universe, pool, request, "pinned_inventory_snapshot")


def _admitted_v2_pool(request: V2QuoteRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    universe = json.loads(request.universe.read_text(encoding="utf-8"))
    if universe.get("chain_id") != 57073:
        raise ValueError("universe is not for Ink")
    pool_address = request.pool.lower()
    pool = next(
        (
            candidate
            for candidate in universe.get("candidates", [])
            if candidate.get("venue") == "velodrome_v2" and str(candidate.get("address", "")).lower() == pool_address
        ),
        None,
    )
    if pool is None:
        raise ValueError("pool is not an admitted Velodrome V2 candidate")
    if pool.get("stable"):
        raise ValueError("stable V2 pools need their own invariant and are intentionally unsupported")
    return universe, pool


def _admitted_slipstream_pool(request: V2QuoteRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    universe = json.loads(request.universe.read_text(encoding="utf-8"))
    if universe.get("chain_id") != 57073:
        raise ValueError("universe is not for Ink")
    pool_address = request.pool.lower()
    pool = next(
        (
            candidate
            for candidate in universe.get("candidates", [])
            if candidate.get("venue") == "slipstream" and str(candidate.get("address", "")).lower() == pool_address
        ),
        None,
    )
    if pool is None:
        raise ValueError("pool is not an admitted Slipstream candidate")
    return universe, pool


def _quote_v2_pool(
    universe: dict[str, Any], pool: dict[str, Any], request: V2QuoteRequest, source: str, **source_fields: Any
) -> dict[str, Any]:
    pool_address = request.pool.lower()
    token_in = request.token_in.lower()
    token0, token1 = str(pool["token0"]).lower(), str(pool["token1"]).lower()
    if token_in == token0:
        token_out, reserve_in, reserve_out = token1, int(pool["reserve0"]), int(pool["reserve1"])
    elif token_in == token1:
        token_out, reserve_in, reserve_out = token0, int(pool["reserve1"]), int(pool["reserve0"])
    else:
        raise ValueError("token_in is not a side of the selected pool")
    amount_out = constant_product_out(request.amount_in, reserve_in, reserve_out, int(pool["fee_bps"]))
    if amount_out is None:
        raise ValueError("pool has no quoteable reserves or fee")
    return {
        "chain_id": 57073,
        "source": source,
        "inventory_block_number": universe["inventory_block_number"],
        "inventory_block_timestamp": universe["inventory_block_timestamp"],
        "venue": "velodrome_v2",
        "pool": pool_address,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": str(request.amount_in),
        "amount_out": str(amount_out),
        "fee_bps": int(pool["fee_bps"]),
        "assumptions": [
            "volatile constant-product pair",
            "input amount reaches pair unchanged",
            "current state must still be validated at transaction inclusion",
        ],
        **source_fields,
    }


async def quote_v2_live(
    request: V2QuoteRequest, rpc_url: str = INK_RPC, max_reserve_age_seconds: int = 3600
) -> dict[str, Any]:
    """Read V2 reserves and dynamic fee together at one current sealed block."""
    if max_reserve_age_seconds < 0:
        raise ValueError("max_reserve_age_seconds must not be negative")
    universe, admitted = _admitted_v2_pool(request)
    pool_address = str(admitted["address"]).lower()
    async with ReadOnlyRpc(rpc_url) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError(f"could not read pinned block {block}")
        calls = [
            ReadCall(pool_address, encode_call("getReserves()"), "reserves"),
            ReadCall(
                VELODROME_V2_FACTORY,
                encode_call("getFee(address,bool)", ["address", "bool"], [pool_address, False]),
                "fee",
            ),
        ]
        replies = await rpc.multicall(calls, block)
    values: dict[str, bytes] = {}
    for call, (success, data) in zip(calls, replies, strict=True):
        if not success:
            raise RuntimeError(f"current V2 read failed: {call.label}")
        values[call.label] = data
    reserve0, reserve1, reserve_timestamp = decode_result(["uint256", "uint256", "uint256"], values["reserves"])
    quote_timestamp = int(sealed["timestamp"], 16)
    reserve_age = max(0, quote_timestamp - reserve_timestamp)
    if reserve_age > max_reserve_age_seconds:
        raise ValueError(f"V2 reserves are stale by {reserve_age} seconds")
    current_pool = {
        **admitted,
        "reserve0": str(reserve0),
        "reserve1": str(reserve1),
        "fee_bps": decode_result(["uint256"], values["fee"])[0],
    }
    return _quote_v2_pool(
        universe,
        current_pool,
        request,
        "sealed_rpc_read",
        quote_block_number=block,
        quote_block_timestamp=quote_timestamp,
        reserve_timestamp=reserve_timestamp,
        reserve_age_seconds=reserve_age,
        max_reserve_age_seconds=max_reserve_age_seconds,
    )


SLIPSTREAM_QUOTER = "0x426ef6f781ba0fbc1a7b0d3399d6fa6548464c85"


def decode_slipstream_quote(data: bytes) -> dict[str, int]:
    amount_out, sqrt_price_x96_after, initialized_ticks_crossed, gas_estimate = decode_result(
        ["uint256", "uint160", "uint32", "uint256"], data
    )
    return {
        "amount_out": amount_out,
        "sqrt_price_x96_after": sqrt_price_x96_after,
        "initialized_ticks_crossed": initialized_ticks_crossed,
        "gas_estimate": gas_estimate,
    }


async def quote_slipstream_live(request: V2QuoteRequest, rpc_url: str = INK_RPC) -> dict[str, Any]:
    """Use the deployed Slipstream quoter at one sealed block after policy checks."""
    if request.amount_in <= 0:
        raise ValueError("amount_in must be positive")
    universe, admitted = _admitted_slipstream_pool(request)
    pool_address = str(admitted["address"]).lower()
    token_in = request.token_in.lower()
    async with ReadOnlyRpc(rpc_url) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError(f"could not read pinned block {block}")
        calls = [
            ReadCall(pool_address, encode_call("token0()"), "token0"),
            ReadCall(pool_address, encode_call("token1()"), "token1"),
            ReadCall(pool_address, encode_call("tickSpacing()"), "tick_spacing"),
            ReadCall(pool_address, encode_call("liquidity()"), "liquidity"),
            ReadCall(SLIPSTREAM_FACTORY, encode_call("getSwapFee(address)", ["address"], [pool_address]), "fee"),
        ]
        replies = await rpc.multicall(calls, block)
        values: dict[str, bytes] = {}
        for call, (success, data) in zip(calls, replies, strict=True):
            if not success:
                raise RuntimeError(f"current Slipstream read failed: {call.label}")
            values[call.label] = data
        token0 = decode_result(["address"], values["token0"])[0].lower()
        token1 = decode_result(["address"], values["token1"])[0].lower()
        spacing = decode_result(["int24"], values["tick_spacing"])[0]
        liquidity = decode_result(["uint128"], values["liquidity"])[0]
        if (token0, token1, spacing) != (
            str(admitted["token0"]).lower(), str(admitted["token1"]).lower(), int(admitted["tick_spacing"])
        ):
            raise RuntimeError("Slipstream pool identity differs from the admitted inventory")
        if liquidity <= 0:
            raise ValueError("Slipstream pool has no current liquidity")
        if token_in == token0:
            token_out = token1
        elif token_in == token1:
            token_out = token0
        else:
            raise ValueError("token_in is not a side of the selected pool")
        params = (token_in, token_out, request.amount_in, spacing, 0)
        quote_data = await rpc.contract_call(
            SLIPSTREAM_QUOTER,
            encode_call(
                "quoteExactInputSingle((address,address,uint256,int24,uint160))",
                ["(address,address,uint256,int24,uint160)"],
                [params],
            ),
            block,
        )
    quote = decode_slipstream_quote(bytes.fromhex(quote_data[2:]))
    if quote["amount_out"] <= 0:
        raise ValueError("Slipstream quoter returned zero output")
    return {
        "chain_id": 57073,
        "source": "sealed_rpc_quoter",
        "inventory_block_number": universe["inventory_block_number"],
        "inventory_block_timestamp": universe["inventory_block_timestamp"],
        "quote_block_number": block,
        "quote_block_timestamp": int(sealed["timestamp"], 16),
        "venue": "slipstream",
        "pool": pool_address,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": str(request.amount_in),
        "amount_out": str(quote["amount_out"]),
        "tick_spacing": spacing,
        "liquidity": str(liquidity),
        "fee_pips": decode_result(["uint256"], values["fee"])[0],
        "sqrt_price_x96_after": str(quote["sqrt_price_x96_after"]),
        "initialized_ticks_crossed": quote["initialized_ticks_crossed"],
        "gas_estimate": quote["gas_estimate"],
        "assumptions": [
            "Slipstream Quoter result is a read-only estimate",
            "current state must still be validated at transaction inclusion",
        ],
    }
