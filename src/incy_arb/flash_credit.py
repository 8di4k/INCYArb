"""Read-only capacity snapshot for Uniswap v4 flash accounting on Ink."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .abi import ReadCall, decode_result, encode_call
from .observer import INK_RPC
from .rpc import ReadOnlyRpc


POOL_MANAGER = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
INT128_MAX = (1 << 127) - 1
NATIVE_CURRENCY = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class FlashCapacityConfig:
    tokens: tuple[str, ...]
    output: Path = Path("data/ink-flash-capacity.json")
    rpc_url: str = INK_RPC


def format_units(raw: int, decimals: int | None) -> str | None:
    if decimals is None:
        return None
    whole, fraction = divmod(raw, 10**decimals)
    if not fraction:
        return str(whole)
    return f"{whole}.{fraction:0{decimals}d}".rstrip("0")


async def snapshot_flash_capacity(config: FlashCapacityConfig) -> dict[str, Any]:
    """Read PoolManager balances; this is a physical ceiling, not a promise of safe execution."""
    tokens = tuple(dict.fromkeys(token.lower() for token in config.tokens))
    if not tokens or any(len(token) != 42 or not token.startswith("0x") for token in tokens):
        raise ValueError("at least one 20-byte token address is required")
    async with ReadOnlyRpc(config.rpc_url) as rpc:
        if await rpc.chain_id() != 57073:
            raise ValueError("flash capacity RPC must have Ink chain ID 57073")
        block = await rpc.block_number()
        sealed = await rpc.sealed_block(block)
        if sealed is None or not sealed.get("hash"):
            raise RuntimeError("could not pin a sealed block")
        calls: list[ReadCall] = []
        for token in tokens:
            if token == NATIVE_CURRENCY:
                continue
            calls.extend(
                (
                    ReadCall(token, encode_call("balanceOf(address)", ["address"], [POOL_MANAGER]), f"{token}:balance"),
                    ReadCall(token, encode_call("decimals()"), f"{token}:decimals", allow_failure=True),
                    ReadCall(token, encode_call("symbol()"), f"{token}:symbol", allow_failure=True),
                )
            )
        replies = await rpc.multicall(calls, block)
        native_balance = await rpc.native_balance(POOL_MANAGER, block) if NATIVE_CURRENCY in tokens else None
        after = await rpc.sealed_block(block)
        if not after or after.get("hash") != sealed["hash"]:
            raise RuntimeError("capacity block changed during reads")
    raw = {call.label: result for call, result in zip(calls, replies, strict=True)}
    balances: list[dict[str, Any]] = []
    for token in tokens:
        if token == NATIVE_CURRENCY:
            if native_balance is None:
                raise RuntimeError("native balance unavailable")
            balance, decimals, symbol = native_balance, 18, "ETH"
        else:
            balance_ok, balance_data = raw[f"{token}:balance"]
            if not balance_ok:
                raise RuntimeError(f"balanceOf failed for {token}")
            balance = decode_result(["uint256"], balance_data)[0]
            decimals_ok, decimals_data = raw[f"{token}:decimals"]
            symbol_ok, symbol_data = raw[f"{token}:symbol"]
            try:
                decimals = decode_result(["uint256"], decimals_data)[0] if decimals_ok else None
            except Exception:
                decimals = None
            try:
                symbol = decode_result(["string"], symbol_data)[0] if symbol_ok else None
            except Exception:
                symbol = None
        balances.append(
            {
                "token": token,
                "currency_kind": "native" if token == NATIVE_CURRENCY else "erc20",
                "symbol": symbol,
                "decimals": decimals,
                "pool_manager_balance": str(balance),
                "take_accounting_limit": str(INT128_MAX),
                "maximum_single_take": str(min(balance, INT128_MAX)),
                "display_balance": format_units(balance, decimals),
                "has_physical_flash_capacity": balance > 0,
            }
        )
    output = {
        "chain_id": 57073,
        "pool_manager": POOL_MANAGER,
        "block_number": block,
        "block_hash": sealed["hash"],
        "block_timestamp": int(sealed["timestamp"], 16),
        "generated_at_unix": time.time(),
        "balances": balances,
        "warning": (
            "Capacity is capped by both the currency balance and PoolManager's signed-int128 accounting. "
            "Apply a safety margin, token policy, "
            "route liquidity, pending-state checks, and exact execution simulation before using it."
        ),
    }
    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output
