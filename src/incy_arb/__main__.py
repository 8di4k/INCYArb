from __future__ import annotations

import argparse
import asyncio
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
    inventory = subparsers.add_parser("inventory", help="write a pinned V2 and Slipstream pool inventory")
    inventory.add_argument("--output", type=Path, default=Path("data/ink-pools.json"))
    inventory.add_argument("--rpc-url", default=InventoryConfig.rpc_url)
    inventory.add_argument("--batch-size", type=int, default=InventoryConfig.batch_size)
    inventory.add_argument("--max-stale-seconds", type=int, default=InventoryConfig.max_stale_seconds)
    v4 = subparsers.add_parser("v4-inventory", help="checkpointed discovery of PoolManager Initialize events")
    v4.add_argument("--output", type=Path, default=V4DiscoveryConfig.output)
    v4.add_argument("--max-pages", type=int, default=V4DiscoveryConfig.max_pages)
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
    flash = subparsers.add_parser("flash-capacity", help="snapshot PoolManager token balances at one sealed block")
    flash.add_argument("--token", action="append", required=True)
    flash.add_argument("--output", type=Path, default=FlashCapacityConfig.output)
    flash.add_argument("--rpc-url", default=INK_RPC)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "observe":
            result = asyncio.run(observe(ObserverConfig(args.ws_url, args.rpc_url, args.duration, args.output)))
        elif args.command == "inventory":
            inventory_result = asyncio.run(
                build_inventory(InventoryConfig(args.rpc_url, args.output, args.batch_size, args.max_stale_seconds))
            )
            result = {"output": str(args.output), "block_number": inventory_result["block_number"], "summary": inventory_result["summary"]}
        elif args.command == "v4-inventory":
            v4_result = asyncio.run(discover_v4_pools(V4DiscoveryConfig(args.output, max_pages=args.max_pages, restart=args.restart)))
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
            )))
        else:
            flash_result = asyncio.run(snapshot_flash_capacity(
                FlashCapacityConfig(tuple(args.token), args.output, args.rpc_url)
            ))
            result = {"output": str(args.output), "block_number": flash_result["block_number"],
                      "balances": flash_result["balances"]}
    except KeyboardInterrupt:
        return 130
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
