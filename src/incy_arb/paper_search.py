"""Offline, gross-only cycle sizing over canonical-checked subblock evidence."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .flash_credit import INT128_MAX
from .local_scan import LocalV4Config, size_local_paths
from .research import cycle_paths
from .subblock_replay import captured_payload, replay_payload
from .v4_replay import restore_snapshot

PRICING_FIELDS = ("sqrt_price_x96", "tick", "liquidity", "tick_spacing", "lp_fee", "protocol_fee", "tick_window")


def pricing(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record[field] for field in PRICING_FIELDS}


def paper_search(baseline: dict[str, Any], replay: dict[str, Any]) -> dict[str, Any]:
    """No RPC, signing, fee assessment, or execution validation occurs here."""
    if baseline.get("chain_id") != 57073 or replay.get("chain_id") != 57073:
        raise ValueError("Ink baseline and replay required")
    if replay.get("source") != "posthoc_canonical_subblock_v4_replay":
        raise ValueError("subblock replay evidence required")
    if baseline["quote_block_number"] != replay["start_block"]:
        raise ValueError("baseline and replay start block differ")
    block_hash = baseline["quote_block_hash"]
    models = {key: restore_snapshot(r) for key, r in baseline["snapshots"].items()}
    if not models or any(
        s.block != replay["start_block"] or s.block_hash != block_hash or s.pool["address"] != key
        for key, s in models.items()
    ):
        raise ValueError("inconsistent baseline snapshots")
    if not replay["initial_snapshots"] or set(replay["initial_snapshots"]) - set(models):
        raise ValueError("baseline must cover every tracked replay pool")
    for key, record in replay["initial_snapshots"].items():
        if record["block_hash"] != block_hash or pricing(record) != pricing(models[key].record()):
            raise ValueError("initial pricing state differs between baseline and replay")
    options = baseline["sizing_options"]
    starts = baseline["start_tokens"]
    if len(starts) != 1 or not 2 <= options["max_steps"] <= 4:
        raise ValueError("one start currency and 2–4 steps required")
    config = LocalV4Config(
        Path("unused"),
        Path("unused"),
        int(options["minimum"]),
        int(options["maximum"]),
        start_token=starts[0].lower(),
        max_steps=options["max_steps"],
        grid_points=options["grid_points"],
        refinement_iterations=options["refinement_iterations"],
    )
    if (
        not 0 < config.minimum <= config.maximum <= INT128_MAX
        or not 3 <= config.grid_points <= 129
        or not 0 <= config.refinement_iterations <= 128
    ):
        raise ValueError("invalid bounded paper sizing configuration")
    paths = cycle_paths([(s.venue, s.pool) for s in models.values()], (config.start_token,), config.max_steps)
    identities = [tuple(edge[1]["address"] for edge in path) for path in paths]
    if len(set(identities)) != len(identities):
        raise ValueError("ambiguous duplicate cycle identities")
    started = time.perf_counter()
    initial_search = size_local_paths(paths, models, config)
    active = {tuple(f["pools"]): f for f in initial_search["findings"]}
    initial_findings = list(initial_search["findings"])
    timeline, changes = [], []
    searches = cycles_recomputed = 0
    peak = max((int(f["gross_pnl"]) for f in active.values()), default=0)
    peak_cursor = {"kind": "baseline", "block_number": replay["start_block"]}
    expected_number = replay["start_block"] + 1
    for evidence in replay["blocks"]:
        payload = captured_payload(evidence["capture"])
        if payload.block_number != expected_number:
            raise ValueError("paper replay gap or duplicate block")
        expected_number += 1
        final, prefixes = replay_payload(models, payload, evidence["block"], evidence["receipts"])
        for prefix in prefixes:
            changed = {
                key
                for key, record in prefix["pricing_state_after"].items()
                if pricing(record) != pricing(models[key].record())
            }
            affected = [path for path in paths if any(edge[1]["address"] in changed for edge in path)]
            # Math-only prefix models retain their parent metadata. They must never be passed
            # to sealed simulation. A separate cursor identifies this reconstructed prefix.
            for key, record in prefix["pricing_state_after"].items():
                models[key] = restore_snapshot(
                    dict(record, block_number=models[key].block, block_hash=models[key].block_hash)
                )
            if affected:
                previous = {key: (f["amount_in"], f["gross_pnl"]) for key, f in active.items()}
                # Each state update starts a fresh leg cache. Pool+amount alone is unsafe
                # across prefixes because a changed pool can return a different quote.
                search = size_local_paths(affected, models, config)
                searches += 1
                cycles_recomputed += len(affected)
                for path in affected:
                    active.pop(tuple(edge[1]["address"] for edge in path), None)
                active.update({tuple(f["pools"]): f for f in search["findings"]})
                current = {key: (f["amount_in"], f["gross_pnl"]) for key, f in active.items()}
                changes.append(
                    {
                        "block_number": payload.block_number,
                        "subblock_index": prefix["subblock_index"],
                        "changed_pools": sorted(changed),
                        "cycles_recomputed": len(affected),
                        "new_candidates": sum(key not in previous for key in current),
                        "removed_candidates": sum(key not in current for key in previous),
                        "updated_candidates": sum(
                            key in previous and previous[key] != point for key, point in current.items()
                        ),
                        "findings": sorted(active.values(), key=lambda f: int(f["gross_pnl"]), reverse=True),
                    }
                )
            gross = max((int(f["gross_pnl"]) for f in active.values()), default=0)
            cursor = {"block_number": payload.block_number, "subblock_index": prefix["subblock_index"]}
            if gross > peak:
                peak, peak_cursor = gross, cursor
            timeline.append(
                dict(
                    cursor,
                    changed_pools=len(changed),
                    cycles_recomputed=len(affected),
                    positive_local_gross=len(active),
                    best_gross_pnl=str(gross),
                )
            )
        models = final
    if expected_number - 1 != replay["end_block"]:
        raise ValueError("paper replay endpoint differs")
    for key, reference in replay["canonical_snapshots"].items():
        if key not in replay["initial_snapshots"] or pricing(models[key].record()) != pricing(reference):
            raise ValueError("final paper pricing differs from canonical endpoint evidence")
    if set(replay["canonical_snapshots"]) != set(replay["initial_snapshots"]):
        raise ValueError("missing canonical endpoint evidence")
    return {
        "chain_id": 57073,
        "source": "offline_subblock_gross_paper_search",
        "start_block": replay["start_block"],
        "end_block": replay["end_block"],
        "start_token": config.start_token,
        "baseline_block_hash": block_hash,
        "sizing_options": options,
        "initial_findings": initial_findings,
        "changes": changes,
        "timeline": timeline,
        "final_findings": sorted(active.values(), key=lambda f: int(f["gross_pnl"]), reverse=True),
        "summary": {
            "pools": len(models),
            "cycles": len(paths),
            "subblocks": len(timeline),
            "incremental_searches": searches,
            "cycles_recomputed": cycles_recomputed,
            "initial_positive_gross": len(initial_findings),
            "final_positive_gross": len(active),
            "peak_gross_pnl": str(peak),
            "peak_cursor": peak_cursor,
            "search_seconds": time.perf_counter() - started,
        },
        "warning": "Offline gross-only paper reconstruction from supplied evidence. No independent network revalidation in this command. Prefix candidates have no full-EVM execution or fee confirmation, and no live timing or profitability claim.",
    }
