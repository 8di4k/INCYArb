"""Pinned, read-only v4 StateView snapshot for discovered Ink pool IDs."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .abi import ReadCall, decode_result, encode_call
from .observer import INK_RPC
from .rpc import ReadOnlyRpc


V4_STATE_VIEW = "0x76fd297e2d437cd7f76d50f01afe6160f86e9990"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DYNAMIC_FEE_FLAG = 0x800000


@dataclass(frozen=True)
class V4StateConfig:
    discovery: Path = Path("data/ink-v4-pools.json")
    output: Path = Path("data/ink-v4-state.json")
    rpc_url: str = INK_RPC
    batch_size: int = 30
    requests_per_second: float = 1.0
    max_pools: int | None = None


def decode_slot0(data: bytes) -> dict[str, int]:
    sqrt_price_x96, tick, protocol_fee, lp_fee = decode_result(["uint160", "int24", "uint24", "uint24"], data)
    return {
        "sqrt_price_x96": sqrt_price_x96,
        "tick": tick,
        "protocol_fee": protocol_fee,
        "lp_fee": lp_fee,
    }


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


async def snapshot_v4_state(config: V4StateConfig) -> dict[str, Any]:
    """Load StateView data at one sealed block without trusting storage layout."""
    if config.batch_size < 1:
        raise ValueError("batch_size must be positive")
    if config.max_pools is not None and config.max_pools < 1:
        raise ValueError("max_pools must be positive when set")
    discovery = json.loads(config.discovery.read_text(encoding="utf-8"))
    if discovery.get("chain_id") != 57073 or discovery.get("pool_manager", "").lower() != "0x360e68faccca8ca495c1b759fd9eee466db9fb32":
        raise ValueError("discovery is not an Ink PoolManager checkpoint")
    pools = discovery.get("pools")
    if not isinstance(pools, dict):
        raise ValueError("discovery pools must be a mapping")
    discovered_pool_ids = sorted(pools)
    pool_ids = discovered_pool_ids if config.max_pools is None else discovered_pool_ids[:config.max_pools]
    states: dict[str, dict[str, Any]] = {}
    async with ReadOnlyRpc(config.rpc_url, requests_per_second=config.requests_per_second) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError(f"could not read pinned block {block}")
        for chunk in _chunks(pool_ids, config.batch_size):
            calls: list[ReadCall] = []
            for pool_id in chunk:
                encoded_id = bytes.fromhex(pool_id[2:])
                calls.extend((
                    ReadCall(V4_STATE_VIEW, encode_call("getSlot0(bytes32)", ["bytes32"], [encoded_id]), f"{pool_id}:slot0", True),
                    ReadCall(V4_STATE_VIEW, encode_call("getLiquidity(bytes32)", ["bytes32"], [encoded_id]), f"{pool_id}:liquidity", True),
                ))
            replies = await rpc.multicall(calls, block)
            by_label = {call.label: reply for call, reply in zip(calls, replies, strict=True)}
            for pool_id in chunk:
                slot_success, slot_data = by_label[f"{pool_id}:slot0"]
                liquidity_success, liquidity_data = by_label[f"{pool_id}:liquidity"]
                if not slot_success or not liquidity_success:
                    states[pool_id] = {"readable": False}
                    continue
                slot = decode_slot0(slot_data)
                liquidity = decode_result(["uint128"], liquidity_data)[0]
                definition = pools[pool_id]
                zero_hook = definition.get("hooks", "").lower() == ZERO_ADDRESS
                erc20_currencies = (
                    definition.get("currency0", "").lower() != ZERO_ADDRESS
                    and definition.get("currency1", "").lower() != ZERO_ADDRESS
                )
                static_fee = not (int(definition.get("fee_pips", 0)) & DYNAMIC_FEE_FLAG)
                states[pool_id] = {
                    "readable": True,
                    **{key: str(value) if key == "sqrt_price_x96" else value for key, value in slot.items()},
                    "liquidity": str(liquidity),
                    "has_positive_liquidity": liquidity > 0,
                    "zero_hook": zero_hook,
                    "erc20_currencies": erc20_currencies,
                    "static_fee": static_fee,
                    "eligible_for_future_quote": liquidity > 0 and zero_hook and erc20_currencies and static_fee,
                }
    result = {
        "schema_version": 1,
        "chain_id": 57073,
        "state_view": V4_STATE_VIEW,
        "discovery_complete": bool(discovery.get("complete")),
        "discovery_pages_scanned": discovery.get("pages_scanned"),
        "block_number": block,
        "block_timestamp": int(sealed["timestamp"], 16),
        "generated_at_unix": time.time(),
        "states": states,
        "summary": {
            "discovered_pool_ids": len(discovered_pool_ids),
            "selected_pool_ids": len(pool_ids),
            "readable": sum(state["readable"] for state in states.values()),
            "positive_liquidity": sum(state.get("has_positive_liquidity", False) for state in states.values()),
            "eligible_for_future_quote": sum(state.get("eligible_for_future_quote", False) for state in states.values()),
        },
    }
    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
