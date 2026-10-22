"""Pinned, read-only inventory of major Ink AMM factories."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .abi import ReadCall, decode_result, encode_call
from .observer import INK_RPC
from .rpc import ReadOnlyRpc

VELODROME_V2_FACTORY = "0x31832f2a97fd20664d76cc421207669b55ce4bc0"
SLIPSTREAM_FACTORY = "0x718e46d0962a66942e233760a8bd6038ce54edcd"
UNISWAP_V2_FACTORY = "0xfe57a6ba1951f69ae2ed4abe23e0f095df500c04"
DYORSWAP_FACTORY = "0x6c86ab200661512fdbd27da4bb87df15609a2806"


@dataclass(frozen=True)
class InventoryConfig:
    rpc_url: str = INK_RPC
    output: Path = Path("data/ink-pools.json")
    batch_size: int = 40
    max_stale_seconds: int = 3600


def _address(data: bytes) -> str:
    return decode_result(["address"], data)[0].lower()


def _uint(data: bytes) -> int:
    return decode_result(["uint256"], data)[0]


def _bool(data: bytes) -> bool:
    return decode_result(["bool"], data)[0]


def _string(data: bytes) -> str:
    return decode_result(["string"], data)[0]


def _chunks(values: list[ReadCall], size: int) -> Iterable[list[ReadCall]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


async def _run_calls(rpc: ReadOnlyRpc, calls: list[ReadCall], block: int, batch_size: int) -> dict[str, bytes | None]:
    results: dict[str, bytes | None] = {}
    for chunk in _chunks(calls, batch_size):
        replies = await rpc.multicall(chunk, block)
        for call, (success, data) in zip(chunk, replies, strict=True):
            if not success and not call.allow_failure:
                raise RuntimeError(f"required call failed: {call.label}")
            results[call.label] = data if success else None
    return results


async def _pool_addresses(rpc: ReadOnlyRpc, factory: str, block: int, batch_size: int) -> list[str]:
    length_data = await rpc.contract_call(factory, encode_call("allPoolsLength()"), block)
    length = _uint(bytes.fromhex(length_data[2:]))
    calls = [
        ReadCall(factory, encode_call("allPools(uint256)", ["uint256"], [index]), f"pool:{index}")
        for index in range(length)
    ]
    replies = await _run_calls(rpc, calls, block, batch_size)
    return [_address(replies[f"pool:{index}"]) for index in range(length)]


async def _pair_addresses(rpc: ReadOnlyRpc, factory: str, block: int, batch_size: int) -> list[str]:
    length_data = await rpc.contract_call(factory, encode_call("allPairsLength()"), block)
    length = _uint(bytes.fromhex(length_data[2:]))
    calls = [
        ReadCall(factory, encode_call("allPairs(uint256)", ["uint256"], [index]), f"pair:{index}")
        for index in range(length)
    ]
    replies = await _run_calls(rpc, calls, block, batch_size)
    return [_address(replies[f"pair:{index}"]) for index in range(length)]


async def _token_metadata(
    rpc: ReadOnlyRpc, tokens: set[str], block: int, batch_size: int
) -> dict[str, dict[str, Any]]:
    calls: list[ReadCall] = []
    for token in sorted(tokens):
        calls += [
            ReadCall(token, encode_call("decimals()"), f"{token}:decimals", allow_failure=True),
            ReadCall(token, encode_call("symbol()"), f"{token}:symbol", allow_failure=True),
        ]
    raw = await _run_calls(rpc, calls, block, batch_size)
    out: dict[str, dict[str, Any]] = {}
    for token in tokens:
        decimals = raw[f"{token}:decimals"]
        symbol = raw[f"{token}:symbol"]
        try:
            decimal_value = _uint(decimals) if decimals else None
        except Exception:  # non-standard ERC-20
            decimal_value = None
        try:
            symbol_value = _string(symbol) if symbol else None
        except Exception:  # bytes32 symbols are intentionally not guessed
            symbol_value = None
        out[token] = {"decimals": decimal_value, "symbol": symbol_value}
    return out


async def _v2_pools(
    rpc: ReadOnlyRpc, block: int, block_timestamp: int, batch_size: int, max_stale_seconds: int
) -> list[dict[str, Any]]:
    pools = await _pool_addresses(rpc, VELODROME_V2_FACTORY, block, batch_size)
    calls: list[ReadCall] = []
    for pool in pools:
        calls += [
            ReadCall(pool, encode_call("token0()"), f"{pool}:token0"),
            ReadCall(pool, encode_call("token1()"), f"{pool}:token1"),
            ReadCall(pool, encode_call("stable()"), f"{pool}:stable"),
            ReadCall(pool, encode_call("getReserves()"), f"{pool}:reserves"),
        ]
    raw = await _run_calls(rpc, calls, block, batch_size)
    fees = [
        ReadCall(
            VELODROME_V2_FACTORY,
            encode_call("getFee(address,bool)", ["address", "bool"], [pool, _bool(raw[f"{pool}:stable"])]),
            f"{pool}:fee",
        )
        for pool in pools
    ]
    fee_data = await _run_calls(rpc, fees, block, batch_size)
    out: list[dict[str, Any]] = []
    for pool in pools:
        reserve0, reserve1, timestamp = decode_result(["uint256", "uint256", "uint256"], raw[f"{pool}:reserves"])
        reserve_age = max(0, block_timestamp - timestamp)
        out.append({
            "address": pool,
            "token0": _address(raw[f"{pool}:token0"]),
            "token1": _address(raw[f"{pool}:token1"]),
            "stable": _bool(raw[f"{pool}:stable"]),
            "reserve0": str(reserve0),
            "reserve1": str(reserve1),
            "reserve_timestamp": timestamp,
            "reserve_age_seconds": reserve_age,
            "fee_bps": _uint(fee_data[f"{pool}:fee"]),
            "has_nonzero_reserves": reserve0 > 0 and reserve1 > 0,
            "recently_updated": reserve_age <= max_stale_seconds,
        })
    return out


async def _uniswap_style_v2_pools(
    rpc: ReadOnlyRpc,
    factory: str,
    block: int,
    block_timestamp: int,
    batch_size: int,
    max_stale_seconds: int,
    fee_bps: int | None,
) -> list[dict[str, Any]]:
    """Read the common Uniswap-V2 pair surface without guessing fork-specific fees."""
    pools = await _pair_addresses(rpc, factory, block, batch_size)
    calls: list[ReadCall] = []
    for pool in pools:
        calls += [
            ReadCall(pool, encode_call("token0()"), f"{pool}:token0"),
            ReadCall(pool, encode_call("token1()"), f"{pool}:token1"),
            ReadCall(pool, encode_call("getReserves()"), f"{pool}:reserves"),
        ]
    raw = await _run_calls(rpc, calls, block, batch_size)
    out: list[dict[str, Any]] = []
    for pool in pools:
        reserve0, reserve1, timestamp = decode_result(["uint112", "uint112", "uint32"], raw[f"{pool}:reserves"])
        reserve_age = max(0, block_timestamp - timestamp)
        out.append({
            "address": pool,
            "factory": factory,
            "token0": _address(raw[f"{pool}:token0"]),
            "token1": _address(raw[f"{pool}:token1"]),
            "reserve0": str(reserve0),
            "reserve1": str(reserve1),
            "reserve_timestamp": timestamp,
            "reserve_age_seconds": reserve_age,
            "fee_bps": fee_bps,
            "has_nonzero_reserves": reserve0 > 0 and reserve1 > 0,
            "recently_updated": reserve_age <= max_stale_seconds,
        })
    return out


async def _slipstream_pools(rpc: ReadOnlyRpc, block: int, batch_size: int) -> list[dict[str, Any]]:
    pools = await _pool_addresses(rpc, SLIPSTREAM_FACTORY, block, batch_size)
    calls: list[ReadCall] = []
    for pool in pools:
        calls += [
            ReadCall(pool, encode_call("token0()"), f"{pool}:token0"),
            ReadCall(pool, encode_call("token1()"), f"{pool}:token1"),
            ReadCall(pool, encode_call("tickSpacing()"), f"{pool}:spacing"),
            ReadCall(pool, encode_call("liquidity()"), f"{pool}:liquidity"),
            ReadCall(pool, encode_call("slot0()"), f"{pool}:slot0"),
            ReadCall(SLIPSTREAM_FACTORY, encode_call("getSwapFee(address)", ["address"], [pool]), f"{pool}:fee"),
        ]
    raw = await _run_calls(rpc, calls, block, batch_size)
    out: list[dict[str, Any]] = []
    for pool in pools:
        # Slipstream deployments expose the v3-compatible leading words. Some
        # pool implementations do not expose every later observation field,
        # which discovery does not need; decoding only the mandatory prefix
        # keeps an ABI variation from making the whole inventory unusable.
        sqrt_price, tick = decode_result(["uint160", "int24"], raw[f"{pool}:slot0"])
        liquidity = _uint(raw[f"{pool}:liquidity"])
        out.append({
            "address": pool,
            "token0": _address(raw[f"{pool}:token0"]),
            "token1": _address(raw[f"{pool}:token1"]),
            "tick_spacing": decode_result(["int24"], raw[f"{pool}:spacing"])[0],
            "liquidity": str(liquidity),
            "sqrt_price_x96": str(sqrt_price),
            "tick": tick,
            "fee_pips": _uint(raw[f"{pool}:fee"]),
            "has_positive_liquidity": liquidity > 0,
        })
    return out


async def build_inventory(config: InventoryConfig) -> dict[str, Any]:
    """Write a single block-pinned inventory; no transaction is constructed or sent."""
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError(f"could not read pinned block {block}")
        block_timestamp = int(sealed["timestamp"], 16)
        v2, slipstream, uniswap_v2, dyorswap_v2 = await asyncio.gather(
            _v2_pools(rpc, block, block_timestamp, config.batch_size, config.max_stale_seconds),
            _slipstream_pools(rpc, block, config.batch_size),
            _uniswap_style_v2_pools(
                rpc, UNISWAP_V2_FACTORY, block, block_timestamp, config.batch_size,
                config.max_stale_seconds, 30,
            ),
            _uniswap_style_v2_pools(
                rpc, DYORSWAP_FACTORY, block, block_timestamp, config.batch_size,
                config.max_stale_seconds, None,
            ),
        )
        tokens = {
            pool[side]
            for pool in v2 + slipstream + uniswap_v2 + dyorswap_v2
            for side in ("token0", "token1")
        }
        token_metadata = await _token_metadata(rpc, tokens, block, config.batch_size)
    output = {
        "chain_id": 57073,
        "block_number": block,
        "block_timestamp": block_timestamp,
        "generated_at_unix": time.time(),
        "tokens": token_metadata,
        "velodrome_v2": v2,
        "slipstream": slipstream,
        "uniswap_v2": uniswap_v2,
        "dyorswap_v2": dyorswap_v2,
        "summary": {
            "velodrome_v2_total": len(v2),
            "velodrome_v2_nonzero_reserves": sum(pool["has_nonzero_reserves"] for pool in v2),
            "velodrome_v2_recently_updated": sum(pool["recently_updated"] for pool in v2),
            "slipstream_total": len(slipstream),
            "slipstream_positive_liquidity": sum(pool["has_positive_liquidity"] for pool in slipstream),
            "uniswap_v2_total": len(uniswap_v2),
            "uniswap_v2_nonzero_reserves": sum(pool["has_nonzero_reserves"] for pool in uniswap_v2),
            "uniswap_v2_recently_updated": sum(pool["recently_updated"] for pool in uniswap_v2),
            "dyorswap_v2_total": len(dyorswap_v2),
            "dyorswap_v2_nonzero_reserves": sum(pool["has_nonzero_reserves"] for pool in dyorswap_v2),
            "dyorswap_v2_recently_updated": sum(pool["recently_updated"] for pool in dyorswap_v2),
            "freshness_window_seconds": config.max_stale_seconds,
        },
    }
    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output
