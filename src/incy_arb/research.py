"""Read-only structural route research.  Output is never an execution instruction."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .abi import ReadCall, decode_result, encode_call
from .inventory import VELODROME_V2_FACTORY
from .observer import INK_RPC
from .quotes import SLIPSTREAM_QUOTER, constant_product_out, decode_slipstream_quote
from .rpc import ReadOnlyRpc, RpcError
from .flash_credit import POOL_MANAGER


VELODROME_ROUTER = "0x3a63171dd9bebf4d07bc782fecc7eb0b890c2a45"


@dataclass(frozen=True)
class TwoHopConfig:
    inventory: Path
    amounts: tuple[int, ...]
    rpc_url: str = INK_RPC


@dataclass(frozen=True)
class ThreeHopConfig(TwoHopConfig):
    pass


@dataclass(frozen=True)
class CycleScanConfig(TwoHopConfig):
    v4_discovery: Path = Path("data/ink-v4-pools.json")
    v4_state: Path = Path("data/ink-v4-state.json")
    starts: tuple[str, ...] = ("0x0200c29006150606b650577bbe7b6248f58470c1",)
    max_steps: int = 4
    min_steps: int = 2
    start_amounts: tuple[tuple[str, int], ...] = ()


def overlapping_slipstream_pairs(inventory: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for pool in inventory.get("slipstream", []):
        if pool.get("has_positive_liquidity"):
            groups[tuple(sorted((pool["token0"].lower(), pool["token1"].lower())))].append(pool)
    return [(pools[i], pools[j]) for pools in groups.values() for i in range(len(pools)) for j in range(i + 1, len(pools))]


async def _quote(rpc: ReadOnlyRpc, block: int, token_in: str, token_out: str, amount: int, spacing: int) -> dict[str, int]:
    data = encode_call(
        "quoteExactInputSingle((address,address,uint256,int24,uint160))",
        ["(address,address,uint256,int24,uint160)"],
        [(token_in, token_out, amount, spacing, 0)],
    )
    raw = await rpc.contract_call(SLIPSTREAM_QUOTER, data, block)
    return decode_slipstream_quote(bytes.fromhex(raw[2:]))


async def scan_two_hop_slipstream(config: TwoHopConfig) -> dict[str, Any]:
    if not config.amounts or any(amount <= 0 for amount in config.amounts):
        raise ValueError("amounts must be positive")
    inventory = json.loads(config.inventory.read_text(encoding="utf-8"))
    if inventory.get("chain_id") != 57073:
        raise ValueError("inventory is not for Ink")
    routes = overlapping_slipstream_pairs(inventory)
    findings: list[dict[str, Any]] = []
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError("could not pin a sealed block")
        for first, second in routes:
            for entry, middle in ((first, second), (second, first)):
                directions = ((entry["token0"].lower(), entry["token1"].lower()), (entry["token1"].lower(), entry["token0"].lower()))
                for token_in, token_middle in directions:
                    for amount in config.amounts:
                        one = await _quote(rpc, block, token_in, token_middle, amount, int(entry["tick_spacing"]))
                        two = await _quote(rpc, block, token_middle, token_in, one["amount_out"], int(middle["tick_spacing"]))
                        findings.append({
                            "entry_pool": entry["address"].lower(), "exit_pool": middle["address"].lower(),
                            "token_in": token_in, "token_middle": token_middle, "amount_in": str(amount),
                            "amount_out": str(two["amount_out"]), "gross_pnl": str(two["amount_out"] - amount),
                            "profitable_before_gas": two["amount_out"] > amount,
                            "quoter_gas_estimate": one["gas_estimate"] + two["gas_estimate"],
                        })
    return {
        "chain_id": 57073, "source": "sealed_rpc_quoter", "quote_block_number": block,
        "quote_block_timestamp": int(sealed["timestamp"], 16), "inventory_block_number": inventory["block_number"],
        "structural_pairs": len(routes), "findings": findings,
        "warning": "Research only: gross PnL excludes gas conversion, token risk, and execution permission.",
    }


def _research_pools(inventory: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return (
        [("velodrome_v2", pool) for pool in inventory.get("velodrome_v2", [])
         if pool.get("has_nonzero_reserves") and pool.get("recently_updated")]
        + [("uniswap_v2", pool) for pool in inventory.get("uniswap_v2", [])
           if pool.get("has_nonzero_reserves") and pool.get("recently_updated") and pool.get("fee_bps") is not None]
        + [("slipstream", pool) for pool in inventory.get("slipstream", []) if pool.get("has_positive_liquidity")]
    )


def _three_hop_paths(pools: list[tuple[str, dict[str, Any]]]) -> list[list[tuple[str, dict[str, Any], str, str]]]:
    directed = [(venue, pool, token_in, token_out) for venue, pool in pools
                for token_in, token_out in ((pool["token0"].lower(), pool["token1"].lower()),
                                             (pool["token1"].lower(), pool["token0"].lower()))]
    paths: list[list[tuple[str, dict[str, Any], str, str]]] = []
    for first in directed:
        for second in directed:
            if first[3] != second[2] or first[1]["address"] == second[1]["address"]:
                continue
            for third in directed:
                if second[3] == third[2] and third[3] == first[2] and third[1]["address"] not in (first[1]["address"], second[1]["address"]):
                    paths.append([first, second, third])
    return paths


def cycle_paths(
    pools: list[tuple[str, dict[str, Any]]], starts: tuple[str, ...], max_steps: int
) -> list[list[tuple[str, dict[str, Any], str, str]]]:
    """Port of Arbitr's simple-cycle DFS: 2..max_steps, no repeated venue/token."""
    by_in: dict[str, list[tuple[str, dict[str, Any], str, str]]] = defaultdict(list)
    for venue, pool in pools:
        for token_in, token_out in ((pool["token0"].lower(), pool["token1"].lower()),
                                    (pool["token1"].lower(), pool["token0"].lower())):
            by_in[token_in].append((venue, pool, token_in, token_out))
    cycles: list[list[tuple[str, dict[str, Any], str, str]]] = []
    for start in (value.lower() for value in starts):
        stack = [(start, [], frozenset(), frozenset((start,)))]
        while stack:
            token, path, used, seen = stack.pop()
            for edge in by_in.get(token, []):
                venue_key = edge[1]["address"].lower()
                if venue_key in used:
                    continue
                next_path = path + [edge]
                if edge[3] == start:
                    if len(next_path) >= 2:
                        cycles.append(next_path)
                    continue
                if len(next_path) >= max_steps or edge[3] in seen:
                    continue
                stack.append((edge[3], next_path, used | {venue_key}, seen | {edge[3]}))
    return cycles


def xarb2_compatibility(path: list[tuple[str, dict[str, Any], str, str]]) -> tuple[bool, list[str]]:
    """Describe only adapter support already present in Arbitr's XArb2 contract."""
    reasons: list[str] = []
    for venue, pool, _token_in, _token_out in path:
        if venue == "velodrome_v2" and pool.get("stable"):
            reasons.append("velodrome_stable_requires_new_adapter")
        elif venue not in ("velodrome_v2", "uniswap_v2", "slipstream", "uniswap_v4"):
            reasons.append(f"unsupported_venue:{venue}")
    return not reasons, list(dict.fromkeys(reasons))


def _eligible_v4_pools(discovery: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for pool_id, snapshot in state.get("states", {}).items():
        if not snapshot.get("eligible_for_future_quote") or pool_id not in discovery.get("pools", {}):
            continue
        definition = discovery["pools"][pool_id]
        out.append(("uniswap_v4", {
            "address": pool_id, "token0": definition["currency0"], "token1": definition["currency1"],
            "fee_pips": definition["fee_pips"], "tick_spacing": definition["tick_spacing"],
            "hooks": definition["hooks"],
        }))
    return out


async def _quote_v4(rpc: ReadOnlyRpc, block: int, pool: dict[str, Any], token_in: str, amount: int) -> tuple[int, int]:
    key = (pool["token0"], pool["token1"], int(pool["fee_pips"]), int(pool["tick_spacing"]), pool["hooks"])
    data = encode_call(
        "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))",
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
        [(key, token_in == pool["token0"].lower(), amount, b"")],
    )
    raw = await rpc.contract_call("0x3972c00f7ed4885e145823eb7c655375d275a1c5", data, block)
    return decode_result(["uint256", "uint256"], bytes.fromhex(raw[2:]))


async def _current_v2(
    rpc: ReadOnlyRpc, block: int, pools: list[tuple[str, dict[str, Any]]]
) -> dict[str, dict[str, int]]:
    calls: list[ReadCall] = []
    for venue, pool in pools:
        address = pool["address"].lower()
        calls.append(ReadCall(address, encode_call("getReserves()"), f"{address}:reserves"))
        if venue == "velodrome_v2":
            calls.append(ReadCall(
                VELODROME_V2_FACTORY,
                encode_call("getFee(address,bool)", ["address", "bool"], [address, bool(pool.get("stable"))]),
                f"{address}:fee",
            ))
    replies = await rpc.multicall(calls, block)
    data = {call.label: value for call, value in zip(calls, replies, strict=True)}
    out: dict[str, dict[str, int]] = {}
    for venue, pool in pools:
        address = pool["address"].lower()
        reserves_ok, reserves = data[f"{address}:reserves"]
        if venue == "velodrome_v2":
            fee_ok, fee = data[f"{address}:fee"]
            fee_bps = decode_result(["uint256"], fee)[0] if fee_ok else None
        else:
            fee_bps = pool.get("fee_bps")
        if reserves_ok and fee_bps is not None:
            r0, r1, _ = decode_result(["uint256", "uint256", "uint256"], reserves)
            out[address] = {"reserve0": r0, "reserve1": r1, "fee_bps": int(fee_bps)}
    return out


async def _flash_balances(rpc: ReadOnlyRpc, block: int, tokens: tuple[str, ...]) -> dict[str, int]:
    unique = tuple(dict.fromkeys(token.lower() for token in tokens))
    calls = [
        ReadCall(token, encode_call("balanceOf(address)", ["address"], [POOL_MANAGER]), token)
        for token in unique
    ]
    replies = await rpc.multicall(calls, block)
    return {
        token: decode_result(["uint256"], data)[0]
        for token, (success, data) in zip(unique, replies, strict=True)
        if success
    }


async def _quote_stable_v2(rpc: ReadOnlyRpc, block: int, token_in: str, token_out: str, amount: int) -> int:
    data = encode_call(
        "getAmountsOut(uint256,(address,address,bool)[])",
        ["uint256", "(address,address,bool)[]"],
        [amount, [(token_in, token_out, True)]],
    )
    raw = await rpc.contract_call(VELODROME_ROUTER, data, block)
    return decode_result(["uint256[]"], bytes.fromhex(raw[2:]))[0][-1]


async def scan_three_hop(config: ThreeHopConfig) -> dict[str, Any]:
    inventory = json.loads(config.inventory.read_text(encoding="utf-8"))
    if inventory.get("chain_id") != 57073 or not config.amounts or any(amount <= 0 for amount in config.amounts):
        raise ValueError("Ink inventory and positive amounts are required")
    pools = _research_pools(inventory)
    paths = _three_hop_paths(pools)
    findings: list[dict[str, Any]] = []
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError("could not pin a sealed block")
        current_v2 = await _current_v2(
            rpc, block, [(venue, pool) for venue, pool in pools if venue in ("velodrome_v2", "uniswap_v2")]
        )
        for path in paths:
            for amount in config.amounts:
                value, gas = amount, 0
                valid = True
                for venue, pool, token_in, token_out in path:
                    if venue in ("velodrome_v2", "uniswap_v2"):
                        state = current_v2.get(pool["address"].lower())
                        if state is None:
                            valid = False; break
                        if pool.get("stable"):
                            try:
                                value = await _quote_stable_v2(rpc, block, token_in, token_out, value)
                            except RpcError:
                                valid = False; break
                        else:
                            reserve_in, reserve_out = (state["reserve0"], state["reserve1"]) if token_in == pool["token0"].lower() else (state["reserve1"], state["reserve0"])
                            value = constant_product_out(value, reserve_in, reserve_out, state["fee_bps"])
                            if value is None:
                                valid = False; break
                    else:
                        quote = await _quote(rpc, block, token_in, pool["token1"].lower() if token_in == pool["token0"].lower() else pool["token0"].lower(), value, int(pool["tick_spacing"]))
                        value, gas = quote["amount_out"], gas + quote["gas_estimate"]
                if valid and value is not None:
                    findings.append({"tokens": [path[0][2], path[0][3], path[1][3], path[2][3]],
                                     "pools": [entry[1]["address"].lower() for entry in path], "venues": [entry[0] for entry in path],
                                     "amount_in": str(amount), "amount_out": str(value), "gross_pnl": str(value - amount),
                                     "profitable_before_gas": value > amount, "slipstream_quoter_gas_estimate": gas})
    return {"chain_id": 57073, "source": "sealed_rpc_read_and_quoter", "quote_block_number": block,
            "quote_block_timestamp": int(sealed["timestamp"], 16), "structural_paths": len(paths), "findings": findings,
            "warning": "Research only: V2 transfer-tax, all gas conversion, token risk, and execution permission are excluded."}


async def scan_cycles(config: CycleScanConfig) -> dict[str, Any]:
    if not 2 <= config.min_steps <= config.max_steps <= 8:
        raise ValueError("cycle step range must satisfy 2 <= min_steps <= max_steps <= 8")
    amounts_by_start: dict[str, list[int]] = defaultdict(list)
    for token, amount in config.start_amounts:
        amounts_by_start[token.lower()].append(amount)
    if any(amount <= 0 for amount in config.amounts) or any(
        amount <= 0 for amounts in amounts_by_start.values() for amount in amounts
    ):
        raise ValueError("amounts must be positive")
    missing_amounts = [start for start in config.starts if start.lower() not in amounts_by_start and not config.amounts]
    if missing_amounts:
        raise ValueError(f"no amount configured for start tokens: {', '.join(missing_amounts)}")
    inventory = json.loads(config.inventory.read_text(encoding="utf-8"))
    discovery = json.loads(config.v4_discovery.read_text(encoding="utf-8"))
    state = json.loads(config.v4_state.read_text(encoding="utf-8"))
    pools = _research_pools(inventory) + _eligible_v4_pools(discovery, state)
    paths = [path for path in cycle_paths(pools, config.starts, config.max_steps) if len(path) >= config.min_steps]
    findings: list[dict[str, Any]] = []
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None:
            raise RuntimeError("could not pin a sealed block")
        current_v2 = await _current_v2(
            rpc, block, [(venue, pool) for venue, pool in pools if venue in ("velodrome_v2", "uniswap_v2")]
        )
        flash_balances = await _flash_balances(rpc, block, config.starts)
        for path in paths:
            path_amounts = (tuple(amounts_by_start[path[0][2]])
                            if path[0][2] in amounts_by_start else config.amounts)
            for amount in path_amounts:
                value, quoted_gas, valid = amount, 0, True
                try:
                    for venue, pool, token_in, token_out in path:
                        if venue in ("velodrome_v2", "uniswap_v2"):
                            current = current_v2.get(pool["address"].lower())
                            if current is None:
                                valid = False; break
                            if pool.get("stable"):
                                value = await _quote_stable_v2(rpc, block, token_in, token_out, value)
                            else:
                                r_in, r_out = ((current["reserve0"], current["reserve1"])
                                               if token_in == pool["token0"].lower()
                                               else (current["reserve1"], current["reserve0"]))
                                value = constant_product_out(value, r_in, r_out, current["fee_bps"])
                                if value is None:
                                    valid = False; break
                        elif venue == "slipstream":
                            quote = await _quote(rpc, block, token_in, token_out, value, int(pool["tick_spacing"]))
                            value, quoted_gas = quote["amount_out"], quoted_gas + quote["gas_estimate"]
                        else:
                            value, gas = await _quote_v4(rpc, block, pool, token_in, value)
                            quoted_gas += gas
                except RpcError:
                    valid = False
                if valid and value is not None:
                    compatible, compatibility_reasons = xarb2_compatibility(path)
                    findings.append({
                        "tokens": [path[0][2]] + [edge[3] for edge in path],
                        "pools": [edge[1]["address"].lower() for edge in path],
                        "venues": [edge[0] for edge in path], "steps": len(path),
                        "amount_in": str(amount), "amount_out": str(value), "gross_pnl": str(value - amount),
                        "profitable_before_gas": value > amount, "quoter_gas_estimate": quoted_gas,
                        "xarb2_execution_compatible": compatible,
                        "xarb2_incompatibility_reasons": compatibility_reasons,
                        "pool_manager_balance": str(flash_balances.get(path[0][2], 0)),
                        "flash_capacity_sufficient": amount <= flash_balances.get(path[0][2], 0),
                    })
    return {
        "chain_id": 57073, "source": "sealed_rpc_multi_venue_quotes", "quote_block_number": block,
        "quote_block_timestamp": int(sealed["timestamp"], 16), "start_tokens": list(config.starts),
        "min_steps": config.min_steps, "max_steps": config.max_steps,
        "pool_manager": POOL_MANAGER,
        "start_flash_capacity": {token: str(amount) for token, amount in flash_balances.items()},
        "structural_cycles": len(paths), "findings": findings,
        "warning": "Research only: gross PnL excludes total execution gas, token risk, and execution permission.",
    }
