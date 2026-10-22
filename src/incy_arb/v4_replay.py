"""Atomic v4 pricing-state replay, verified against independent sealed snapshots."""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from eth_abi import decode
from eth_utils import keccak

from .cl_math import MAX_SQRT, MAX_TICK, MIN_SQRT, MIN_TICK, sqrt_at_tick
from .cl_snapshot import BitmapTickMap, CLSnapshot, load_cl_snapshot
from .flash_credit import POOL_MANAGER
from .observer import INK_RPC
from .rpc import ReadOnlyRpc
from .subblocks import _hex_int, _log_contents

log = logging.getLogger(__name__)
SWAP = keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
MODIFY = keccak(text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
PROTOCOL = keccak(text="ProtocolFeeUpdated(bytes32,uint24)")
INITIALIZE = keccak(text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")


def restore_snapshot(record: dict[str, Any]) -> CLSnapshot:
    window = record["tick_window"]
    if record["venue"] != "uniswap_v4":
        raise ValueError("v4 replay requires v4 snapshots")
    net = {int(t): int(n) for t, n in window["liquidity_net"].items()}
    gross = {int(t): int(n) for t, n in window["liquidity_gross"].items()}
    spacing, lo, hi = int(record["tick_spacing"]), int(window["lo"]), int(window["hi"])
    if not 0 < spacing < 1 << 23 or not MIN_TICK <= lo <= hi <= MAX_TICK:
        raise ValueError("invalid snapshot tick window")
    if set(net) != set(gross) or any(
        not 0 < gross[t] < 1 << 128
        or abs(net[t]) > gross[t]
        or not -(1 << 127) <= net[t] < 1 << 127
        or t % spacing
        or not lo <= t <= hi
        for t in net
    ):
        raise ValueError("inconsistent initialized tick net/gross")
    return CLSnapshot(
        record["venue"],
        record["pool"],
        int(record["block_number"]),
        record["block_hash"],
        int(record["sqrt_price_x96"]),
        int(record["tick"]),
        int(record["liquidity"]),
        int(record["tick_spacing"]),
        int(record["lp_fee"]),
        int(record["protocol_fee"]),
        BitmapTickMap(net, int(window["lo"]), int(window["hi"]), int(record["tick_spacing"])),
        gross,
    )


def apply_v4_log(snapshot: CLSnapshot, event: dict[str, Any]) -> tuple[CLSnapshot, str | None]:
    """Copy on write: failed transactions/blocks cannot partially mutate input ticks."""
    address, topics, data = _log_contents(event)
    if address.hex() != POOL_MANAGER[2:] or len(topics) < 2 or topics[1].hex() != snapshot.pool["address"][2:]:
        return snapshot, None
    topic = topics[0]
    if topic == SWAP:
        if len(topics) != 3 or len(data) != 192:
            raise ValueError("malformed v4 Swap")
        _, _, sqrt, liquidity, tick, fee = decode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], data)
        if (
            not MIN_TICK <= tick < MAX_TICK
            or not MIN_SQRT <= sqrt < MAX_SQRT
            or not sqrt_at_tick(tick) <= sqrt <= sqrt_at_tick(tick + 1)
            or fee not in (snapshot.fee(True), snapshot.fee(False))
        ):
            raise ValueError("inconsistent v4 Swap price/tick/fee")
        return replace(snapshot, sqrt_price_x96=sqrt, tick=tick, liquidity=liquidity), "swap"
    if topic == PROTOCOL:
        if len(topics) != 2 or len(data) != 32:
            raise ValueError("malformed protocol fee event")
        fee = decode(["uint24"], data)[0]
        if (fee & 0xFFF) > 1000 or fee >> 12 > 1000:
            raise ValueError("invalid directional protocol fee")
        return replace(snapshot, protocol_fee=fee), "protocol_fee"
    if topic == MODIFY:
        if len(topics) != 3 or len(data) != 128:
            raise ValueError("malformed ModifyLiquidity")
        lower, upper, delta, _ = decode(["int24", "int24", "int256", "bytes32"], data)
        if (
            not MIN_TICK <= lower < upper <= MAX_TICK
            or lower % snapshot.tick_spacing
            or upper % snapshot.tick_spacing
            or not -(1 << 127) <= delta < 1 << 127
        ):
            raise ValueError("invalid liquidity range/delta")
        if delta == 0:
            return snapshot, "liquidity_zero"
        net, gross = dict(snapshot.ticks.net), dict(snapshot.liquidity_gross)
        for boundary, change in ((lower, delta), (upper, -delta)):
            if not snapshot.ticks.lo <= boundary <= snapshot.ticks.hi:
                continue
            g, n = gross.get(boundary, 0) + delta, net.get(boundary, 0) + change
            if not 0 <= g < 1 << 128 or not -(1 << 127) <= n < 1 << 127 or abs(n) > g:
                raise ValueError("liquidity tick overflow/underflow")
            if g:
                gross[boundary], net[boundary] = g, n
            else:
                gross.pop(boundary, None)
                net.pop(boundary, None)
        liquidity = snapshot.liquidity + (delta if lower <= snapshot.tick < upper else 0)
        if not 0 <= liquidity < 1 << 128:
            raise ValueError("active liquidity overflow/underflow")
        ticks = BitmapTickMap(net, snapshot.ticks.lo, snapshot.ticks.hi, snapshot.tick_spacing)
        return replace(snapshot, ticks=ticks, liquidity_gross=gross, liquidity=liquidity), "liquidity"
    if topic == INITIALIZE:
        raise ValueError("existing pool cannot be initialized again")
    return snapshot, None  # Donate/Collect and unrelated events do not change pricing state.


def ordered_receipt_logs(block: dict[str, Any], receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Require the complete canonical receipt set and exact global log order."""
    expected = [h.lower() for h in block["transactions"]]
    by_hash = {r["transactionHash"].lower(): r for r in receipts}
    if len(by_hash) != len(receipts) or set(by_hash) != set(expected) or len(set(expected)) != len(expected):
        raise ValueError("receipt transaction set differs from block")
    logs = []
    for index, tx_hash in enumerate(expected):
        receipt = by_hash[tx_hash]
        if (
            _hex_int(receipt["blockNumber"]) != _hex_int(block["number"])
            or receipt["blockHash"].lower() != block["hash"].lower()
            or _hex_int(receipt["transactionIndex"]) != index
        ):
            raise ValueError("receipt block/hash/index mismatch")
        status, events = _hex_int(receipt["status"]), receipt["logs"]
        if status not in (0, 1) or not isinstance(events, list) or (status == 0 and events):
            raise ValueError("invalid receipt status/logs")
        for event in events:
            _log_contents(event)
            if (
                _hex_int(event["blockNumber"]) != _hex_int(block["number"])
                or event["blockHash"].lower() != block["hash"].lower()
                or event["transactionHash"].lower() != tx_hash
                or _hex_int(event["transactionIndex"]) != index
                or _hex_int(event["logIndex"]) != len(logs)
            ):
                raise ValueError("log block/hash/order mismatch")
            logs.append(event)
    return logs


def apply_block(snapshots: dict[str, CLSnapshot], block: dict[str, Any], events: list[dict[str, Any]]):
    """A contiguous block is one atomic state transition; callers verify log provenance."""
    number, block_hash = _hex_int(block["number"]), block["hash"]
    if not snapshots or any(
        s.block != number - 1 or s.block_hash.lower() != block["parentHash"].lower() for s in snapshots.values()
    ):
        raise ValueError("replay block gap or parent hash mismatch")
    updated, counts = dict(snapshots), {}
    for event in events:
        address, topics, _ = _log_contents(event)
        if address.hex() != POOL_MANAGER[2:] or len(topics) < 2:
            continue
        key = "0x" + topics[1].hex()
        if key not in updated:
            continue
        updated[key], kind = apply_v4_log(updated[key], event)
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return {key: replace(s, block=number, block_hash=block_hash) for key, s in updated.items()}, counts


async def verify_v4_replay(scan: dict[str, Any], blocks: int, rpc_url: str = INK_RPC) -> dict[str, Any]:
    if scan.get("chain_id") != 57073 or not 1 <= blocks <= 128:
        raise ValueError("Ink scan and 1–128 replay blocks required")
    initial = {key: restore_snapshot(r) for key, r in scan["snapshots"].items()}
    start, start_hash = int(scan["quote_block_number"]), scan["quote_block_hash"]
    if not initial or any(
        s.block != start or s.block_hash != start_hash or s.pool["address"] != key for key, s in initial.items()
    ):
        raise ValueError("mixed initial snapshots")
    snapshots = initial
    evidence, totals = [], {}
    async with ReadOnlyRpc(rpc_url) as rpc:
        if await rpc.chain_id() != 57073:
            raise ValueError("replay RPC must be Ink")
        before = await rpc.sealed_block(start)
        if not before or before["hash"] != start_hash:
            raise RuntimeError("initial replay block replaced")
        for number in range(start + 1, start + blocks + 1):
            block = await rpc.sealed_block(number)
            if not block or _hex_int(block["number"]) != number:
                raise RuntimeError("replay sealed block unavailable")
            receipts = await rpc.block_receipts(number)
            events = ordered_receipt_logs(block, receipts)
            updated, counts = apply_block(snapshots, block, events)
            after = await rpc.sealed_block(number)
            if not after or after["hash"] != block["hash"]:
                raise RuntimeError("replay block replaced during receipt reads")
            snapshots = updated
            for kind, count in counts.items():
                totals[kind] = totals.get(kind, 0) + count
            evidence.append({"block": block, "receipts": receipts, "applied_events": counts})
            log.info("replayed block %s/%s; selected pool events=%s", number, start + blocks, counts)
        comparisons, canonical = await compare_endpoint(rpc, initial, snapshots)
        start_after, end_after = await rpc.sealed_block(start), await rpc.sealed_block(start + blocks)
        if (
            not start_after
            or start_after["hash"] != start_hash
            or not end_after
            or end_after["hash"] != snapshots[next(iter(snapshots))].block_hash
        ):
            raise RuntimeError("replay chain replaced during endpoint verification")
    return {
        "chain_id": 57073,
        "source": "canonical_v4_receipt_replay",
        "start_block": start,
        "end_block": start + blocks,
        "initial_snapshots": scan["snapshots"],
        "blocks": evidence,
        "replayed_snapshots": {key: s.record() for key, s in snapshots.items()},
        "canonical_snapshots": canonical,
        "comparisons": comparisons,
        "summary": {
            "pools": len(snapshots),
            "blocks": blocks,
            "applied_events": totals,
            "matches": sum(c["status"] == "match" for c in comparisons.values()),
            "differences": sum(c["status"] == "difference" for c in comparisons.values()),
        },
        "warning": "Sealed pricing-state replay only; no live timing or execution profitability claim.",
    }


async def compare_endpoint(rpc: ReadOnlyRpc, initial: dict[str, CLSnapshot], snapshots: dict[str, CLSnapshot]):
    """Independently load endpoint pricing state in each original bitmap window."""
    comparisons, canonical = {}, {}
    for index, (key, state) in enumerate(snapshots.items(), 1):
        original = initial[key]
        # Preserve the original bitmap window even if the current tick moves across a word.
        width = original.ticks.hi - original.ticks.lo + 1
        # Recover words_around from the original window, including TickMath limit clamping.
        middle = (original.tick // original.tick_spacing) >> 8
        words = next(
            (
                w
                for w in range(17)
                if max(MIN_TICK, (middle - w) * 256 * original.tick_spacing) == original.ticks.lo
                and min(MAX_TICK, (middle + w + 1) * 256 * original.tick_spacing - 1) == original.ticks.hi
            ),
            None,
        )
        if width <= 0 or words is None:
            raise ValueError("initial snapshot has an unsupported bitmap window")
        reference = await load_cl_snapshot(
            rpc, "uniswap_v4", state.pool, state.block, words, window_tick=original.tick
        )
        if reference.block_hash != state.block_hash:
            raise RuntimeError("replay endpoint block replaced")
        actual, expected = state.record(), reference.record()
        comparisons[key] = {
            "status": "match" if actual == expected else "difference",
            "different_fields": [field for field in actual if actual[field] != expected[field]],
        }
        canonical[key] = expected
        if index % 10 == 0 or index == len(snapshots):
            log.info("checked replay endpoint %s/%s pools", index, len(snapshots))
    return comparisons, canonical


def read_scan(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
