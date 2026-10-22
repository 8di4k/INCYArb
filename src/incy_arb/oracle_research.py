"""Pinned inventory and quote probes for the identified Ink Kaliber deployment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_utils import is_address, keccak

from .abi import decode_result, encode_call, hex_bytes
from .observer import INK_RPC
from .rpc import ReadOnlyRpc, RpcError

KALIBER_INK = "0xf639cf213b63f7e77d699ff686d591c0ba55fc63"
DEPLOYMENT_SOURCE = "https://github.com/DefiLlama/dimension-adapters/blob/5710ba6f10002d89bdf6c5698e5d7c9d50e4d1d9/dexs/aptos-caliber-prop-amm/index.ts"
ABI_SOURCE = "https://github.com/KyberNetwork/kyberswap-dex-lib/blob/b450e6ddc421104cafc4facd686ea55d1b0b7139/pkg/liquidity-source/caliberprop/abi/Caliber.json"


@dataclass(frozen=True)
class OracleSnapshotConfig:
    inventory: Path
    venue: str = KALIBER_INK
    block: int | None = None
    rpc_url: str = INK_RPC
    page_size: int = 32
    max_pages: int = 32


def resolve_pair_ids(pair_ids: list[bytes], assets: dict[str, Any]):
    """Resolve only exact keccak(base||quote) matches. Unknown pairs stay unresolved."""
    wanted = set(pair_ids)
    resolved = {}
    if not wanted:
        return resolved
    addresses = sorted({a.lower() for a in assets if is_address(a) and int(a, 16)})
    for base in addresses:
        for quote in addresses:
            if base == quote:
                continue
            identity = keccak(bytes.fromhex(base[2:] + quote[2:]))
            if identity in wanted:
                resolved[identity] = (base, quote)
                wanted.remove(identity)
                if not wanted:
                    return resolved
    return resolved


def _rpc_failure(error: RpcError) -> dict[str, Any]:
    data = error.data
    if isinstance(data, dict):
        data = data.get("data", data.get("result"))
    return {
        "status": "rpc_error" if error.code != 3 else "quote_reverted",
        "rpc_code": error.code,
        "error": str(error),
        "revert_data": data,
        "error_selector": data[:10] if isinstance(data, str) and data.startswith("0x") else None,
    }


async def snapshot_oracle_venue(config: OracleSnapshotConfig) -> dict[str, Any]:
    if (
        not is_address(config.venue)
        or not int(config.venue, 16)
        or not 1 <= config.page_size <= 128
        or not 1 <= config.max_pages <= 64
        or (config.block is not None and config.block < 0)
    ):
        raise ValueError("valid venue, bounded pagination and nonnegative block required")
    inventory = json.loads(config.inventory.read_text())
    if inventory.get("chain_id") != 57073:
        raise ValueError("Ink inventory required")
    assets = {a.lower(): m for a, m in inventory.get("tokens", {}).items()}
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        if await rpc.chain_id() != 57073:
            raise ValueError("oracle research RPC must be Ink")
        block = await rpc.block_number() if config.block is None else config.block
        before = await rpc.sealed_block(block)
        if not before or not before.get("hash"):
            raise RuntimeError("cannot pin oracle venue block")
        code = await rpc.code_at(config.venue, block)
        if code == "0x":
            raise ValueError("no contract at the candidate venue on this block")
        pair_ids, seen = [], set()
        complete = False
        for page in range(config.max_pages):
            raw = await rpc.contract_call(
                config.venue,
                encode_call(
                    "getAllPairIds(uint256,uint256)",
                    ["uint256", "uint256"],
                    [page * config.page_size, config.page_size],
                ),
                block,
            )
            ids = list(decode_result(["bytes32[]"], hex_bytes(raw))[0])
            if len(ids) > config.page_size or len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError("invalid or repeated oracle pair pagination")
            pair_ids.extend(ids)
            seen.update(ids)
            if len(ids) < config.page_size:
                complete = True
                break
        resolved = resolve_pair_ids(pair_ids, assets)
        pools = []
        for identity in pair_ids:
            pair = {
                "pair_id": "0x" + identity.hex(),
                "tokens_resolved": identity in resolved,
                "quotes": [],
                "execution_verified": False,
            }
            raw = await rpc.contract_call(
                config.venue, encode_call("getPoolBalances(bytes32)", ["bytes32"], [identity]), block
            )
            balances = decode_result(["uint256", "uint256"], hex_bytes(raw))
            pair["balances"] = [str(x) for x in balances]
            if identity in resolved:
                base, quote = resolved[identity]
                pair.update(base=base, quote=quote, base_metadata=assets.get(base), quote_metadata=assets.get(quote))
                for token_in, token_out, reserve in ((base, quote, balances[0]), (quote, base, balances[1])):
                    for amount in sorted({reserve // 1000, reserve // 100} - {0}):
                        attempt = {"token_in": token_in, "token_out": token_out, "amount_in": str(amount)}
                        try:
                            raw = await rpc.contract_call(
                                config.venue,
                                encode_call(
                                    "quote(bytes32,address,address,uint256)",
                                    ["bytes32", "address", "address", "uint256"],
                                    [identity, token_in, token_out, amount],
                                ),
                                block,
                            )
                            output = decode_result(["uint256"], hex_bytes(raw))[0]
                            attempt.update(
                                status="positive_quote" if output > 0 else "zero_quote", amount_out=str(output)
                            )
                        except RpcError as error:
                            attempt.update(_rpc_failure(error))
                        pair["quotes"].append(attempt)
            pools.append(pair)
        after = await rpc.sealed_block(block)
        if not after or after.get("hash") != before["hash"]:
            raise RuntimeError("oracle venue block replaced during reads")
    attempts = [q for p in pools for q in p["quotes"]]
    return {
        "chain_id": 57073,
        "source": "pinned_oracle_venue_inventory_and_quote_probe",
        "venue": config.venue.lower(),
        "abi_scope": "Kaliber-compatible candidate ABI; deployment reference identifies only the default venue.",
        "block_number": block,
        "block_hash": before["hash"],
        "runtime_code_hash": "0x" + keccak(hex_bytes(code)).hex(),
        "runtime_code_bytes": len(hex_bytes(code)),
        "enumeration_complete": complete,
        "asset_scope": "Input inventory token addresses only; unmatched registered pair IDs stay unresolved. Symbols do not certify token issuers.",
        "pools": pools,
        "references": {"deployment": DEPLOYMENT_SOURCE, "abi": ABI_SOURCE},
        "summary": {
            "registered_pairs": len(pair_ids),
            "resolved_pairs": len(resolved),
            "positive_balances_pairs": sum(all(int(x) > 0 for x in p["balances"]) for p in pools),
            "quote_attempts": len(attempts),
            "positive_quotes": sum(q["status"] == "positive_quote" for q in attempts),
            "reverted_quotes": sum(q["status"] == "quote_reverted" for q in attempts),
        },
        "strategy_status": "unconfirmed",
        "warning": "Research only. Deployment and quotes do not establish oracle-lag edge, access/fee terms, atomic swap execution, issuer authenticity or net profitability. Pool-only routes are diagnostics, outside the target oracle-venue strategy.",
    }
