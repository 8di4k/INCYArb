"""Read-only pinned follow-up for issuer-confirmed xStocks and discovered v4 hooks.

Explorer data is supplied as an offline archive; names/source availability never
qualify a venue. The result records exact runtime equality and remaining unknowns.
"""

import argparse
import asyncio
import gzip
import json
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak

from incy_arb.abi import ReadCall, decode_result, encode_call
from incy_arb.observer import INK_RPC
from incy_arb.rpc import ReadOnlyRpc
from incy_arb.v4_state import V4_STATE_VIEW, decode_slot0

POOL_MANAGER = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"


def read_json(path):
    raw = Path(path).read_bytes()
    return json.loads(gzip.decompress(raw) if str(path).endswith(".gz") else raw)


async def inspect(discovery, assets, explorer, rpc_url, block=None):
    if discovery.get("chain_id") != 57073 or discovery.get("pool_manager", "").lower() != POOL_MANAGER:
        raise ValueError("Ink v4 discovery required")
    catalog_complete = assets.get("complete") if isinstance(assets, dict) else None
    if isinstance(assets, dict):
        assets = [asset for page in assets["pages"] for asset in page["nodes"]]
    tokens = {}
    for asset in assets:
        for deployment in asset.get("deployments", []):
            if deployment.get("network") == "Ink":
                for field in ("address", "wrapperAddress", "wrapperAddressV2"):
                    if deployment.get(field):
                        tokens[deployment[field].lower()] = {"symbol": asset["symbol"], "representation": field}
    if not tokens:
        raise ValueError("issuer metadata has no Ink deployments")
    selected = {}
    for identity, pool in discovery["pools"].items():
        if not {pool["currency0"], pool["currency1"]}.intersection(tokens):
            continue
        encoded = encode(
            ["address", "address", "uint24", "int24", "address"],
            [pool["currency0"], pool["currency1"], pool["fee_pips"], pool["tick_spacing"], pool["hooks"]],
        )
        if "0x" + keccak(encoded).hex() != identity:
            raise ValueError("discovery pool key does not match its ID")
        selected[identity] = pool
    hooks = sorted({p["hooks"] for p in selected.values() if int(p["hooks"], 16)})
    states, contracts = {}, {}
    async with ReadOnlyRpc(rpc_url) as rpc:
        if await rpc.chain_id() != 57073:
            raise ValueError("Ink RPC required")
        block = await rpc.block_number() if block is None else block
        before = await rpc.sealed_block(block)
        if not before or not before.get("hash"):
            raise RuntimeError("cannot pin block")
        ids = sorted(selected)
        for start in range(0, len(ids), 24):
            calls = []
            for identity in ids[start : start + 24]:
                for signature in ("getSlot0(bytes32)", "getLiquidity(bytes32)"):
                    calls.append(
                        ReadCall(
                            V4_STATE_VIEW, encode_call(signature, ["bytes32"], [bytes.fromhex(identity[2:])]), "", True
                        )
                    )
            replies = await rpc.multicall(calls, block)
            if len(replies) != len(calls):
                raise RuntimeError("incomplete multicall")
            for offset, identity in enumerate(ids[start : start + 24]):
                slot, liquidity = replies[offset * 2 : offset * 2 + 2]
                states[identity] = {"readable": slot[0] and liquidity[0]}
                if states[identity]["readable"]:
                    states[identity].update(decode_slot0(slot[1]))
                    states[identity]["liquidity"] = str(decode_result(["uint128"], liquidity[1])[0])
        for hook in hooks:
            code = await rpc.code_at(hook, block)
            entry = explorer.get(hook, {})
            contracts[hook] = {
                "runtime_code_hash": "0x" + keccak(bytes.fromhex(code[2:])).hex(),
                "runtime_code_bytes": len(bytes.fromhex(code[2:])),
                "explorer_runtime_matches": entry.get("deployed_bytecode", "").lower() == code.lower(),
                "explorer_name": entry.get("name"),
                "source_available": bool(entry.get("source_code")),
                "source_hash": "0x" + keccak(text=entry["source_code"]).hex() if entry.get("source_code") else None,
                "explorer_implementations_unverified_at_pinned_block": entry.get("implementations", []),
                "oracle_venue_qualified": False,
            }
        after = await rpc.sealed_block(block)
        if not after or after.get("hash") != before["hash"]:
            raise RuntimeError("block replaced during reads")
    active_ids = [i for i, s in states.items() if s.get("readable") and int(s.get("liquidity", "0")) > 0]
    active_hooks = {selected[i]["hooks"] for i in active_ids if int(selected[i]["hooks"], 16)}
    return {
        "chain_id": 57073,
        "block_number": block,
        "block_hash": before["hash"],
        "issuer_tokens": tokens,
        "issuer_catalog_complete": catalog_complete,
        "issuer_asset_count": len(assets),
        "discovery_complete_at_checkpoint": discovery.get("complete"),
        "discovery_updated_at_unix": discovery.get("updated_at_unix"),
        "scope": "Only supplied discovery IDs and supplied issuer-confirmed assets; not a complete current-chain venue scan. Positive liquidity is not usable depth. Explorer source/proxy claims are not independent compiler or implementation-slot verification.",
        "pools": {i: {"definition": selected[i], "state": s} for i, s in states.items()},
        "hooks": contracts,
        "summary": {
            "selected_pools": len(selected),
            "positive_liquidity_pools": len(active_ids),
            "selected_hooks": len(hooks),
            "positive_liquidity_hooks": len(active_hooks),
            "source_available_active_hooks": sum(contracts[h]["source_available"] for h in active_hooks),
            "unresolved_active_hooks": sum(not contracts[h]["source_available"] for h in active_hooks),
            "explorer_runtime_matches": sum(c["explorer_runtime_matches"] for c in contracts.values()),
            "qualified_oracle_venues": 0,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--issuer-assets", type=Path, required=True)
    parser.add_argument("--explorer-archive", type=Path, required=True)
    parser.add_argument("--block-number", type=int)
    parser.add_argument("--rpc-url", default=INK_RPC)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(
        inspect(
            read_json(args.discovery),
            read_json(args.issuer_assets),
            read_json(args.explorer_archive),
            args.rpc_url,
            args.block_number,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(result, indent=2) + "\n").encode()
    args.output.write_bytes(gzip.compress(encoded, mtime=0) if str(args.output).endswith(".gz") else encoded)
    print(json.dumps({"block_number": result["block_number"], "summary": result["summary"]}))
