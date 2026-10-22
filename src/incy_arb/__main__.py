from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from .inventory import InventoryConfig, build_inventory
from .observer import INK_RPC, ObserverConfig, observe
from .v4_inventory import V4DiscoveryConfig, discover_v4_pools
from .universe import UniverseConfig, build_trade_universe
from .quotes import V2QuoteRequest, quote_slipstream_live, quote_v2_live, quote_v2_snapshot
from .v4_state import V4StateConfig, snapshot_v4_state
from .research import CycleScanConfig, ThreeHopConfig, TwoHopConfig, scan_cycles, scan_three_hop, scan_two_hop_slipstream
from .flash_credit import FlashCapacityConfig, snapshot_flash_capacity
from .simulation import simulate_scan
from .cl_snapshot import check_scan_cl_parity
from .local_scan import LocalV4Config, scan_local_v4
from .v4_replay import read_scan, verify_v4_replay
from .subblock_replay import verify_subblock_replay
from .paper_search import paper_search
from .timing import summarize_timing, read_timing_capture


def _start_amount(value: str) -> tuple[str, int]:
    token, separator, raw_amount = value.partition("=")
    if not separator or not token or not raw_amount:
        raise argparse.ArgumentTypeError("expected TOKEN=RAW_AMOUNT")
    try:
        amount = int(raw_amount)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("RAW_AMOUNT must be an integer") from exc
    if amount <= 0:
        raise argparse.ArgumentTypeError("RAW_AMOUNT must be positive")
    return token.lower(), amount


def _size_range(value: str) -> tuple[str, int, int]:
    token, separator, bounds = value.partition("=")
    try:
        minimum, maximum = (int(part) for part in bounds.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected TOKEN=MIN_RAW:MAX_RAW") from exc
    if not separator or not token or minimum <= 0 or maximum < minimum:
        raise argparse.ArgumentTypeError("expected TOKEN=MIN_RAW:MAX_RAW with 0 < MIN_RAW <= MAX_RAW")
    return token.lower(), minimum, maximum


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Ink subblock observer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch = subparsers.add_parser("observe", help="capture and verify Ink subblocks")
    watch.add_argument("--duration", type=float, default=60.0, help="seconds; 0 runs until Ctrl-C")
    watch.add_argument("--output", type=Path, default=Path("data/ink-subblocks.jsonl"))
    watch.add_argument("--ws-url", default=ObserverConfig.websocket_url)
    watch.add_argument("--rpc-url", default=ObserverConfig.rpc_url)
    watch.add_argument("--skip-receipt-verification", action="store_true", help="capture hashes only; logs remain unverified")
    inventory = subparsers.add_parser("inventory", help="write a pinned V2 and Slipstream pool inventory")
    inventory.add_argument("--output", type=Path, default=Path("data/ink-pools.json"))
    inventory.add_argument("--rpc-url", default=InventoryConfig.rpc_url)
    inventory.add_argument("--batch-size", type=int, default=InventoryConfig.batch_size)
    inventory.add_argument("--max-stale-seconds", type=int, default=InventoryConfig.max_stale_seconds)
    v4 = subparsers.add_parser("v4-inventory", help="checkpointed discovery of PoolManager Initialize events")
    v4.add_argument("--output", type=Path, default=V4DiscoveryConfig.output)
    v4.add_argument("--max-pages", type=int, default=V4DiscoveryConfig.max_pages)
    v4.add_argument("--timeout-seconds", type=float, default=V4DiscoveryConfig.timeout_seconds)
    v4.add_argument("--attempts", type=int, default=V4DiscoveryConfig.attempts)
    v4.add_argument("--restart", action="store_true", help="discard the local cursor and start from the newest logs")
    universe = subparsers.add_parser("universe", help="apply explicit token and pool allowlists to an inventory")
    universe.add_argument("--inventory", type=Path, default=UniverseConfig.inventory)
    universe.add_argument("--policy", type=Path, default=UniverseConfig.policy)
    universe.add_argument("--output", type=Path, default=UniverseConfig.output)
    quote = subparsers.add_parser("quote-v2", help="quote one admitted volatile V2 pool from a pinned snapshot")
    quote.add_argument("--universe", type=Path, default=Path("data/ink-trade-universe.json"))
    quote.add_argument("--pool", required=True)
    quote.add_argument("--token-in", required=True)
    quote.add_argument("--amount-in", type=int, required=True)
    live_quote = subparsers.add_parser("quote-v2-live", help="quote an admitted volatile V2 pool at a sealed RPC block")
    live_quote.add_argument("--universe", type=Path, default=Path("data/ink-trade-universe.json"))
    live_quote.add_argument("--pool", required=True)
    live_quote.add_argument("--token-in", required=True)
    live_quote.add_argument("--amount-in", type=int, required=True)
    live_quote.add_argument("--rpc-url", default=INK_RPC)
    live_quote.add_argument("--max-reserve-age-seconds", type=int, default=3600)
    slipstream_quote = subparsers.add_parser("quote-slipstream-live", help="quote an admitted Slipstream pool through its Quoter")
    slipstream_quote.add_argument("--universe", type=Path, default=Path("data/ink-trade-universe.json"))
    slipstream_quote.add_argument("--pool", required=True)
    slipstream_quote.add_argument("--token-in", required=True)
    slipstream_quote.add_argument("--amount-in", type=int, required=True)
    slipstream_quote.add_argument("--rpc-url", default=INK_RPC)
    v4_state = subparsers.add_parser("v4-state", help="read current StateView state for checkpointed v4 pool IDs")
    v4_state.add_argument("--discovery", type=Path, default=V4StateConfig.discovery)
    v4_state.add_argument("--output", type=Path, default=V4StateConfig.output)
    v4_state.add_argument("--rpc-url", default=V4StateConfig.rpc_url)
    v4_state.add_argument("--batch-size", type=int, default=V4StateConfig.batch_size)
    v4_state.add_argument("--requests-per-second", type=float, default=V4StateConfig.requests_per_second)
    v4_state.add_argument("--max-pools", type=int, help="read only the first sorted pool IDs from a checkpoint")
    scan = subparsers.add_parser("scan-two-hop", help="research-only two-pool Slipstream quote scan")
    scan.add_argument("--inventory", type=Path, required=True)
    scan.add_argument("--amount", type=int, action="append", required=True)
    scan.add_argument("--rpc-url", default=INK_RPC)
    scan_three = subparsers.add_parser("scan-three-hop", help="research-only V2/Slipstream three-hop quote scan")
    scan_three.add_argument("--inventory", type=Path, required=True)
    scan_three.add_argument("--amount", type=int, action="append", required=True)
    scan_three.add_argument("--rpc-url", default=INK_RPC)
    cycles = subparsers.add_parser("scan-cycles", help="Arbitr-style read-only cycle scan from two to eight steps")
    cycles.add_argument("--inventory", type=Path, required=True)
    cycles.add_argument("--v4-discovery", type=Path, required=True)
    cycles.add_argument("--v4-state", type=Path, required=True)
    cycles.add_argument("--start-token", action="append", required=True)
    cycles.add_argument("--amount", type=int, action="append", default=[], help="fallback raw amount for every start token")
    cycles.add_argument("--start-amount", type=_start_amount, action="append", default=[], metavar="TOKEN=RAW_AMOUNT",
                        help="raw amount for one start token; repeat per token to scan several sizes")
    cycles.add_argument("--size-range", type=_size_range, action="append", default=[], metavar="TOKEN=MIN_RAW:MAX_RAW",
                        help="optimize raw input size for this token instead of fixed amounts; repeat per token")
    cycles.add_argument("--size-grid-points", type=int, default=CycleScanConfig.size_grid_points)
    cycles.add_argument("--size-refinement-iterations", type=int, default=CycleScanConfig.size_refinement_iterations)
    cycles.add_argument("--min-steps", type=int, default=2)
    cycles.add_argument("--max-steps", type=int, default=4)
    cycles.add_argument("--rpc-url", default=INK_RPC)
    cycles.add_argument("--exclude-native-v4", action="store_true", help="retain the earlier ERC-20-only research universe")
    cycles.add_argument("--block-number", type=int, help="replay quotes at a historical sealed block instead of latest")
    cycles.add_argument("--output", type=Path, help="save full scan as JSON for simulate-cycle")
    simulation = subparsers.add_parser("simulate-cycle", help="simulate one full scan finding via sealed eth_call overrides")
    simulation.add_argument("--scan", type=Path, required=True)
    simulation.add_argument("--finding", type=int, default=0)
    simulation.add_argument("--rpc-url", default=INK_RPC)
    simulation.add_argument("--output", type=Path)
    simulation.add_argument("--gas-margin-bps", type=int, default=2000)
    simulation.add_argument("--max-fee-per-gas", type=int, help="wei; defaults to twice the pinned base fee plus priority")
    simulation.add_argument("--priority-fee-per-gas", type=int, default=0, help="wei")
    cl = subparsers.add_parser("cl-parity", help="compare local CL tick arithmetic with a pinned on-chain Quoter")
    cl.add_argument("--scan", type=Path, required=True)
    cl.add_argument("--finding", type=int, default=0)
    cl.add_argument("--step", type=int, default=0)
    cl.add_argument("--amount0", type=int, action="append", required=True, help="raw token0 input; repeat to cover several sizes")
    cl.add_argument("--amount1", type=int, action="append", required=True, help="raw token1 input; repeat to cover several sizes")
    cl.add_argument("--words-around", type=int, default=3)
    cl.add_argument("--rpc-url", default=INK_RPC)
    cl.add_argument("--output", type=Path)
    local = subparsers.add_parser("local-v4-scan", help="size full-fill v4 cycles locally and independently simulate finalists")
    local.add_argument("--v4-discovery", type=Path, required=True)
    local.add_argument("--v4-state", type=Path, required=True)
    local.add_argument("--start-token", default=LocalV4Config.start_token)
    local.add_argument("--currency", action="append", default=[], help="optional explicit currency filter; repeat per address")
    local.add_argument("--min-amount", type=int, required=True)
    local.add_argument("--max-amount", type=int, required=True)
    local.add_argument("--max-fee-pips", type=int, default=10000)
    local.add_argument("--max-steps", type=int, default=3)
    local.add_argument("--words-around", type=int, default=3)
    local.add_argument("--grid-points", type=int, default=33)
    local.add_argument("--refinement-iterations", type=int, default=32)
    local.add_argument("--validate-top", type=int, default=3)
    local.add_argument("--block-number", type=int)
    local.add_argument("--rpc-url", default=INK_RPC)
    local.add_argument("--output", type=Path, default=Path("data/ink-local-v4-scan.json"))
    replay = subparsers.add_parser("v4-replay", help="verify canonical v4 log replay against sealed pricing state")
    replay.add_argument("--scan", type=Path, required=True)
    replay.add_argument("--blocks", type=int, default=20)
    replay.add_argument("--rpc-url", default=INK_RPC)
    replay.add_argument("--output", type=Path, default=Path("data/ink-v4-replay.json"))
    sub_replay = subparsers.add_parser("subblock-v4-replay", help="verify v4 state at captured subblock boundaries")
    sub_replay.add_argument("--capture", type=Path, required=True)
    sub_replay.add_argument("--v4-discovery", type=Path, required=True)
    sub_replay.add_argument("--rpc-url", default=INK_RPC)
    sub_replay.add_argument("--output", type=Path, default=Path("data/ink-subblock-v4-replay.json"))
    paper = subparsers.add_parser("paper-subblock-search", help="offline gross-only sizing over subblock replay evidence")
    paper.add_argument("--baseline", type=Path, required=True)
    paper.add_argument("--replay", type=Path, required=True)
    paper.add_argument("--output", type=Path, default=Path("data/ink-paper-subblock-search.json"))
    timing = subparsers.add_parser("capture-timing", help="summarize local message arrival and verification timing")
    timing.add_argument("--capture", type=Path, required=True)
    timing.add_argument("--output", type=Path, default=Path("data/ink-capture-timing.json"))
    flash = subparsers.add_parser("flash-capacity", help="snapshot PoolManager token balances at one sealed block")
    flash.add_argument("--token", action="append", required=True)
    flash.add_argument("--output", type=Path, default=FlashCapacityConfig.output)
    flash.add_argument("--rpc-url", default=INK_RPC)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "observe":
            result = asyncio.run(observe(ObserverConfig(args.ws_url, args.rpc_url, args.duration, args.output, not args.skip_receipt_verification)))
        elif args.command == "inventory":
            inventory_result = asyncio.run(
                build_inventory(InventoryConfig(args.rpc_url, args.output, args.batch_size, args.max_stale_seconds))
            )
            result = {"output": str(args.output), "block_number": inventory_result["block_number"], "summary": inventory_result["summary"]}
        elif args.command == "v4-inventory":
            v4_result = asyncio.run(discover_v4_pools(V4DiscoveryConfig(args.output, max_pages=args.max_pages, restart=args.restart,
                                                                                timeout_seconds=args.timeout_seconds, attempts=args.attempts)))
            result = {
                "output": str(args.output),
                "pages_scanned": v4_result["pages_scanned"],
                "events_seen": v4_result["events_seen"],
                "initialized_pools": len(v4_result["pools"]),
                "complete": v4_result["complete"],
                "next_page_params": v4_result["next_page_params"],
            }
        elif args.command == "universe":
            universe_result = build_trade_universe(UniverseConfig(args.inventory, args.policy, args.output))
            result = {"output": str(args.output), "summary": universe_result["summary"]}
        elif args.command == "quote-v2":
            result = quote_v2_snapshot(V2QuoteRequest(args.universe, args.pool, args.token_in, args.amount_in))
        elif args.command == "quote-v2-live":
            result = asyncio.run(
                quote_v2_live(
                    V2QuoteRequest(args.universe, args.pool, args.token_in, args.amount_in),
                    args.rpc_url,
                    args.max_reserve_age_seconds,
                )
            )
        elif args.command == "quote-slipstream-live":
            result = asyncio.run(
                quote_slipstream_live(V2QuoteRequest(args.universe, args.pool, args.token_in, args.amount_in), args.rpc_url)
            )
        elif args.command == "v4-state":
            v4_state_result = asyncio.run(
                snapshot_v4_state(
                    V4StateConfig(
                        args.discovery, args.output, args.rpc_url, args.batch_size, args.requests_per_second, args.max_pools
                    )
                )
            )
            result = {"output": str(args.output), "block_number": v4_state_result["block_number"], "summary": v4_state_result["summary"]}
        elif args.command == "scan-two-hop":
            result = asyncio.run(scan_two_hop_slipstream(TwoHopConfig(args.inventory, tuple(args.amount), args.rpc_url)))
        elif args.command == "scan-three-hop":
            result = asyncio.run(scan_three_hop(ThreeHopConfig(args.inventory, tuple(args.amount), args.rpc_url)))
        elif args.command == "scan-cycles":
            result = asyncio.run(scan_cycles(CycleScanConfig(
                args.inventory, tuple(args.amount), args.rpc_url, args.v4_discovery, args.v4_state,
                tuple(args.start_token), args.max_steps, args.min_steps, tuple(args.start_amount),
                tuple(args.size_range), args.size_grid_points, args.size_refinement_iterations,
                quote_block=args.block_number, include_native_v4=not args.exclude_native_v4,
            )))
        elif args.command == "simulate-cycle":
            result = asyncio.run(simulate_scan(
                json.loads(args.scan.read_text()), args.rpc_url, args.finding,
                gas_margin_bps=args.gas_margin_bps, max_fee=args.max_fee_per_gas,
                priority_fee=args.priority_fee_per_gas,
            ))
        elif args.command == "cl-parity":
            result = asyncio.run(check_scan_cl_parity(
                json.loads(args.scan.read_text()), args.rpc_url, args.finding, args.step,
                tuple(args.amount0), tuple(args.amount1), args.words_around,
            ))
        elif args.command == "capture-timing":
            result = summarize_timing(read_timing_capture(args.capture))
        elif args.command == "paper-subblock-search":
            result = paper_search(read_scan(args.baseline), read_scan(args.replay))
        elif args.command == "subblock-v4-replay":
            result = asyncio.run(verify_subblock_replay(args.capture, args.v4_discovery, args.rpc_url))
        elif args.command == "v4-replay":
            result = asyncio.run(verify_v4_replay(read_scan(args.scan), args.blocks, args.rpc_url))
        elif args.command == "local-v4-scan":
            result = asyncio.run(scan_local_v4(LocalV4Config(
                args.v4_discovery,args.v4_state,args.min_amount,args.max_amount,
                start_token=args.start_token,currencies=tuple(args.currency),max_fee_pips=args.max_fee_pips,
                max_steps=args.max_steps,words_around=args.words_around,grid_points=args.grid_points,
                refinement_iterations=args.refinement_iterations,validate_top=args.validate_top,
                quote_block=args.block_number,rpc_url=args.rpc_url,
            )))
        else:
            flash_result = asyncio.run(snapshot_flash_capacity(
                FlashCapacityConfig(tuple(args.token), args.output, args.rpc_url)
            ))
            result = {"output": str(args.output), "block_number": flash_result["block_number"],
                      "balances": flash_result["balances"]}
    except KeyboardInterrupt:
        return 130
    if args.command in ("scan-cycles", "simulate-cycle", "cl-parity", "local-v4-scan", "v4-replay", "subblock-v4-replay", "paper-subblock-search", "capture-timing") and args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        if "summary" in result:
            print({"output": str(args.output), "summary": result["summary"]})
        else:
            print({"output": str(args.output), "status": result.get("status"), "findings": len(result.get("findings", []))})
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
