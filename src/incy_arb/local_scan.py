"""Size full-fill v4 cycles locally, then independently simulate selected candidates."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_utils import is_address

from .cl_snapshot import CLSnapshot, load_cl_snapshot
from .flash_credit import INT128_MAX, NATIVE_CURRENCY, POOL_MANAGER
from .observer import INK_RPC
from .research import _eligible_v4_pools, cycle_paths, ink_compatibility, xarb2_compatibility
from .rpc import ReadOnlyRpc, RpcError
from .simulation import simulate_finding
from .sizing import optimize_size
from .v4_state import V4_STATE_VIEW

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalV4Config:
    discovery: Path
    state: Path
    minimum: int
    maximum: int
    start_token: str = NATIVE_CURRENCY
    currencies: tuple[str, ...] = ()
    max_fee_pips: int = 10_000
    max_steps: int = 3
    words_around: int = 3
    grid_points: int = 33
    refinement_iterations: int = 32
    validate_top: int = 3
    quote_block: int | None = None
    rpc_url: str = INK_RPC


class _LegCache:
    def __init__(self, snapshots: dict[str, CLSnapshot], maximum: int = 50_000):
        self.snapshots = snapshots
        self.maximum = maximum
        self.values: dict[tuple[str, str, int], int | None] = {}
        self.computed = self.hits = 0

    def quote(self, path: list, amount: int) -> int | None:
        value = amount
        for venue, pool, token_in, token_out in path:
            key = (pool["address"], token_in, value)
            if key in self.values:
                self.hits += 1
                output = self.values[key]
            else:
                self.computed += 1
                output = self.snapshots[pool["address"]].quote(token_in == pool["token0"].lower(), value)
                if len(self.values) < self.maximum:
                    self.values[key] = output
            if output is None or output <= 0:
                return None
            value = output
        return value


def size_local_paths(paths: list, snapshots: dict[str, CLSnapshot], config: LocalV4Config) -> dict[str, Any]:
    """Pure CPU search. Snapshots must remain immutable for this call's lifetime."""
    cache = _LegCache(snapshots)
    findings = []
    diagnostics = []
    started = time.perf_counter()
    for path_index, path in enumerate(paths, 1):
        evaluations = modeled = 0
        best_observed = None

        def quote(amount: int) -> int | None:
            nonlocal evaluations, modeled, best_observed
            evaluations += 1
            output = cache.quote(path, amount)
            if output is not None:
                modeled += 1
                point = (output - amount, amount, output)
                if best_observed is None or point > best_observed:
                    best_observed = point
            return output

        best = optimize_size(
            quote,
            config.minimum,
            config.maximum,
            INT128_MAX,
            grid_points=config.grid_points,
            refinement_iterations=config.refinement_iterations,
        )
        pools = [edge[1]["address"] for edge in path]
        diagnostic = {
            "pools": pools,
            "evaluations": evaluations,
            "full_fill_evaluations": modeled,
            "status": "positive_local_gross" if best else "no_positive_full_fill" if modeled else "not_fully_modeled",
        }
        if best_observed is not None:
            profit, amount, output = best_observed
            diagnostic["best_observed"] = {
                "amount_in": str(amount),
                "amount_out": str(output),
                "gross_pnl": str(profit),
            }
        diagnostics.append(diagnostic)
        if path_index % 100 == 0 or path_index == len(paths):
            log.info(
                "local size search %s/%s; positive candidates=%s",
                path_index,
                len(paths),
                len(findings) + (best is not None),
            )
        if best is None:
            continue
        compatible, reasons = xarb2_compatibility(path)
        ink_compatible, ink_reasons = ink_compatibility(path)
        findings.append(
            {
                "tokens": [path[0][2]] + [edge[3] for edge in path],
                "pools": pools,
                "venues": [edge[0] for edge in path],
                "steps": len(path),
                "amount_in": str(best.amount_in),
                "amount_out": str(best.amount_out),
                "gross_pnl": str(best.gross_profit),
                "profitable_before_gas": True,
                "initial_take_required": False,
                "xarb2_execution_compatible": compatible,
                "xarb2_incompatibility_reasons": reasons,
                "ink_execution_compatible": ink_compatible,
                "ink_incompatibility_reasons": ink_reasons,
                "simulation_route": {
                    "start_token": path[0][2],
                    "amount_in": str(best.amount_in),
                    "steps": [{"venue": v, "pool": p, "token_in": a, "token_out": b} for v, p, a, b in path],
                },
            }
        )
    return {
        "findings": sorted(findings, key=lambda p: int(p["gross_pnl"]), reverse=True),
        "diagnostics": diagnostics,
        "search_seconds": time.perf_counter() - started,
        "local_quote_cache": {
            "computed": cache.computed,
            "hits": cache.hits,
            "entries": len(cache.values),
            "maximum": cache.maximum,
        },
    }


async def scan_local_v4(config: LocalV4Config) -> dict[str, Any]:
    start = config.start_token.lower()
    currencies = {currency.lower() for currency in config.currencies}
    if not is_address(start) or any(not is_address(c) for c in currencies) or (currencies and start not in currencies):
        raise ValueError("valid start currency must be included in any explicit currency filter")
    if not 0 < config.minimum <= config.maximum <= INT128_MAX or not 0 <= config.max_fee_pips < 1_000_000:
        raise ValueError("invalid input range or nominal fee cap")
    if not 2 <= config.max_steps <= 4 or not 0 <= config.words_around <= 16 or config.validate_top < 0:
        raise ValueError("require 2–4 steps, 0–16 surrounding words, and nonnegative validation count")
    if (
        config.grid_points < 3
        or config.refinement_iterations < 0
        or (config.quote_block is not None and config.quote_block < 0)
    ):
        raise ValueError("invalid grid/refinement/block configuration")
    discovery = json.loads(config.discovery.read_text())
    state = json.loads(config.state.read_text())
    if discovery.get("chain_id") != 57073 or state.get("chain_id") != 57073:
        raise ValueError("local v4 scan requires Ink discovery and state")
    if (
        discovery.get("pool_manager", "").lower() != POOL_MANAGER
        or state.get("state_view", "").lower() != V4_STATE_VIEW
    ):
        raise ValueError("local v4 scan requires the configured Ink PoolManager and StateView")
    pools = [
        (v, p)
        for v, p in _eligible_v4_pools(discovery, state)
        if int(p["fee_pips"]) <= config.max_fee_pips
        and (not currencies or {p["token0"].lower(), p["token1"].lower()} <= currencies)
    ]
    paths = cycle_paths(pools, (start,), config.max_steps)
    used = {edge[1]["address"] for path in paths for edge in path}
    selected = [(v, p) for v, p in pools if p["address"] in used]
    snapshots = {}
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        if await rpc.chain_id() != 57073:
            raise ValueError("local v4 scan RPC must be Ink")
        block = await rpc.block_number() if config.quote_block is None else config.quote_block
        before = await rpc.sealed_block(block)
        if not before or not before.get("hash"):
            raise RuntimeError("cannot pin local scan block")
        for index, (venue, pool) in enumerate(selected, 1):
            snapshot = await load_cl_snapshot(rpc, venue, pool, block, config.words_around)
            if snapshot.block_hash != before["hash"]:
                raise RuntimeError("CL snapshots belong to different block hashes")
            snapshots[pool["address"]] = snapshot
            if index % 10 == 0 or index == len(selected):
                log.info("loaded local CL snapshot %s/%s", index, len(selected))
        after = await rpc.sealed_block(block)
        if not after or after.get("hash") != before["hash"]:
            raise RuntimeError("block replaced during CL collection")
        result = {
            "chain_id": 57073,
            "source": "sealed_local_v4_full_fill_estimates",
            "quote_block_number": block,
            "quote_block_hash": before["hash"],
            "start_tokens": [start],
            "structural_cycles": len(paths),
            "selected_pools": len(selected),
            "snapshots": {key: snapshot.record() for key, snapshot in snapshots.items()},
            "input_snapshot": {
                "discovery_complete": bool(discovery.get("complete")),
                "state_block_number": state.get("block_number"),
                "currencies": sorted(currencies),
                "max_fee_pips": config.max_fee_pips,
            },
            "sizing_options": {
                "minimum": str(config.minimum),
                "maximum": str(config.maximum),
                "grid_points": config.grid_points,
                "refinement_iterations": config.refinement_iterations,
                "max_steps": config.max_steps,
            },
            "warning": "Local full-fill v4 research only. Unmodeled ranges are rejected. Execution and net profit require independent simulation; future state and fees may change.",
        }
        result.update(size_local_paths(paths, snapshots, config))
        validations = []
        for index in range(min(config.validate_top, len(result["findings"]))):
            try:
                simulation = await simulate_finding(rpc, result, index)
            except RpcError as error:
                simulation = {"status": "rpc_error", "error": str(error), "rpc_code": error.code}
            validations.append({"finding_index": index, "simulation": simulation})
        after = await rpc.sealed_block(block)
        if not after or after.get("hash") != before["hash"]:
            raise RuntimeError("block replaced during candidate validation")
        result["execution_validation"] = validations
        result["summary"] = {
            "structural_cycles": len(paths),
            "positive_local_gross": len(result["findings"]),
            "simulated_success": sum(v["simulation"]["status"] == "success" for v in validations),
            "simulated_positive_after_budget": sum(
                v["simulation"]["status"] == "success"
                and v["simulation"].get("quote_difference") == "0"
                and v["simulation"].get("net_profit_assessment", {}).get("status") == "estimated_positive"
                for v in validations
            ),
        }
    return result
