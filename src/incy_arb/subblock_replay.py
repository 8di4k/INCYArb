"""Post-hoc v4 replay at captured subblock boundaries, with canonical log checks."""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from eth_utils import keccak

from .cl_snapshot import CLSnapshot, load_cl_snapshot
from .flash_credit import POOL_MANAGER
from .observer import INK_RPC
from .rpc import ReadOnlyRpc
from .subblocks import Payload, PayloadCollector, verify_receipts, verify_sealed
from .v4_inventory import INK_POOL_MANAGER
from .v4_replay import MODIFY, PROTOCOL, SWAP, apply_block, apply_v4_log, compare_endpoint, ordered_receipt_logs

log = logging.getLogger(__name__)


def captured_payload(record: dict[str, Any]) -> Payload:
    messages = record["messages"]
    if not messages:
        raise ValueError("empty captured payload")
    payload = PayloadCollector._start(messages[0])
    for message in messages[1:]:
        payload.add(message)
    if not payload.valid or payload.block_number != record["block_number"]:
        raise ValueError("invalid captured payload")
    return payload


def replay_payload(initial: dict[str, CLSnapshot], payload: Payload, block: dict[str, Any], receipts: list):
    """Validate the whole payload before exposing any prefix state; input is never mutated."""
    sealed = verify_sealed(payload, block)
    receipt_check = verify_receipts(payload, receipts, block["hash"])
    if not sealed["verified"] or not receipt_check["verified"]:
        raise ValueError("subblock payload differs from canonical block or receipt logs")
    ordered_receipt_logs(block, receipts)
    apply_block(initial, block, [])  # Enforce parent/hash continuity before processing a prefix.
    states = dict(initial)
    prefixes = []
    for message in payload.messages:
        counts, changed = {}, set()
        # Receipt maps have no transaction ordering. The raw tx list supplies it.
        for raw in message["diff"]["transactions"]:
            tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
            for event in message["metadata"]["receipts"][tx_hash]["logs"]:
                topics = event["topics"]
                if event["address"].lower() != POOL_MANAGER or len(topics) < 2:
                    continue
                key = topics[1].lower()
                if key not in states:
                    continue
                states[key], kind = apply_v4_log(states[key], event)
                if kind:
                    counts[kind] = counts.get(kind, 0) + 1
                    changed.add(key)
        pricing = {}
        for key in sorted(changed):
            record = states[key].record()
            # A prefix is not the state of its sealed block; exclude misleading block tags.
            for field in ("block_number", "block_hash"):
                record.pop(field)
            pricing[key] = record
        prefixes.append(
            {
                "subblock_index": message["index"],
                "transactions": len(message["diff"]["transactions"]),
                "applied_events": counts,
                "pricing_state_after": pricing,
            }
        )
    final = {key: replace(s, block=payload.block_number, block_hash=block["hash"]) for key, s in states.items()}
    return final, prefixes


def read_capture(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    records.sort(key=lambda r: r["block_number"])
    numbers = [r["block_number"] for r in records]
    if not numbers or len(numbers) > 256 or numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        raise ValueError("capture must contain 1–256 unique contiguous complete blocks")
    return records


def tracked_pools(records: list[dict[str, Any]], discovery: dict[str, Any]):
    if discovery.get("chain_id") != 57073 or discovery.get("pool_manager", "").lower() != INK_POOL_MANAGER:
        raise ValueError("Ink PoolManager discovery required")
    seen = set()
    for record in records:
        payload = captured_payload(record)
        for tx in payload.transactions:
            for event in tx.receipt["logs"]:
                topics = event.get("topics", [])
                if event.get("address", "").lower() == POOL_MANAGER and len(topics) >= 2:
                    if topics[0].lower() in {"0x" + t.hex() for t in (SWAP, MODIFY, PROTOCOL)}:
                        seen.add(topics[1].lower())
    pools, excluded = {}, {}
    for key in sorted(seen):
        definition = discovery.get("pools", {}).get(key)
        if not definition:
            excluded[key] = "missing_discovery_key"
        elif int(definition["hooks"], 16) != 0 or not 0 <= int(definition["fee_pips"]) < 1_000_000:
            excluded[key] = "hooks_or_unsupported_fee"
        elif definition["source"]["block_number"] >= records[0]["block_number"]:
            excluded[key] = "initialized_during_capture"
        else:
            pools[key] = dict(definition, address=key, token0=definition["currency0"], token1=definition["currency1"])
    return pools, excluded


async def verify_subblock_replay(capture: Path, discovery_path: Path, rpc_url: str = INK_RPC) -> dict[str, Any]:
    records = read_capture(capture)
    pools, excluded = tracked_pools(records, json.loads(discovery_path.read_text()))
    if not pools:
        raise ValueError("capture contains no supported pre-existing v4 pricing events")
    start, end = records[0]["block_number"] - 1, records[-1]["block_number"]
    initial, evidence = {}, []
    async with ReadOnlyRpc(rpc_url) as rpc:
        if await rpc.chain_id() != 57073:
            raise ValueError("subblock replay RPC must be Ink")
        for key, pool in pools.items():
            initial[key] = await load_cl_snapshot(rpc, "uniswap_v4", pool, start)
        baseline = await rpc.sealed_block(start)
        if not baseline or any(s.block_hash != baseline["hash"] for s in initial.values()):
            raise RuntimeError("mixed initial subblock snapshot hashes")
        states = initial
        for record in records:
            payload = captured_payload(record)
            block = await rpc.sealed_block(payload.block_number)
            if not block:
                raise RuntimeError("subblock sealed block unavailable")
            receipts = await rpc.block_receipts(payload.block_number)
            updated, prefixes = replay_payload(states, payload, block, receipts)
            after = await rpc.sealed_block(payload.block_number)
            if not after or after["hash"] != block["hash"]:
                raise RuntimeError("subblock sealed hash replaced during verification")
            states = updated
            evidence.append({"capture": record, "block": block, "receipts": receipts, "prefixes": prefixes})
            if len(evidence) % 20 == 0 or len(evidence) == len(records):
                log.info("verified subblock replay %s/%s blocks", len(evidence), len(records))
        comparisons, canonical = await compare_endpoint(rpc, initial, states)
        before_after, end_after = await rpc.sealed_block(start), await rpc.sealed_block(end)
        if (
            not before_after
            or before_after["hash"] != baseline["hash"]
            or not end_after
            or any(s.block_hash != end_after["hash"] for s in states.values())
        ):
            raise RuntimeError("subblock chain replaced during endpoint verification")
    totals = {}
    for row in evidence:
        for prefix in row["prefixes"]:
            for kind, count in prefix["applied_events"].items():
                totals[kind] = totals.get(kind, 0) + count
    return {
        "chain_id": 57073,
        "source": "posthoc_canonical_subblock_v4_replay",
        "start_block": start,
        "end_block": end,
        "excluded_pools": excluded,
        "initial_snapshots": {key: s.record() for key, s in initial.items()},
        "blocks": evidence,
        "replayed_snapshots": {key: s.record() for key, s in states.items()},
        "canonical_snapshots": canonical,
        "comparisons": comparisons,
        "summary": {
            "blocks": len(records),
            "subblocks": sum(len(e["prefixes"]) for e in evidence),
            "pools": len(pools),
            "applied_events": totals,
            "matches": sum(c["status"] == "match" for c in comparisons.values()),
            "differences": sum(c["status"] == "difference" for c in comparisons.values()),
        },
        "warning": "Post-hoc reconstruction after full canonical verification. No live availability, latency, prefix execution, or profitability claim. Only supported pools with captured pricing events are tracked.",
    }
