"""Explicit-deny trade universe construction for read-only Ink inventory data."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UniverseConfig:
    inventory: Path = Path("data/ink-pools.json")
    policy: Path = Path("config/ink-allowlist.example.json")
    output: Path = Path("data/ink-trade-universe.json")


def _normal_address(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"{field} must be a 20-byte 0x address")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be hexadecimal") from exc
    return value.lower()


def _address_set(value: Any, field: str) -> set[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return {_normal_address(address, field) for address in value}


def load_policy(path: Path) -> dict[str, set[str]]:
    """Load a deliberately narrow policy.  Empty lists grant no trade access."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("allowlist schema_version must be 1")
    return {
        "tokens": _address_set(raw.get("tokens", []), "tokens"),
        "velodrome_v2_pools": _address_set(raw.get("velodrome_v2_pools", []), "velodrome_v2_pools"),
        "slipstream_pools": _address_set(raw.get("slipstream_pools", []), "slipstream_pools"),
    }


def _rejection(pool: dict[str, Any], reason: str, venue: str) -> dict[str, str]:
    return {"venue": venue, "address": str(pool.get("address", "")).lower(), "reason": reason}


def build_trade_universe(config: UniverseConfig) -> dict[str, Any]:
    """Write candidates that pass both a token and a pool allowlist.

    This is a policy gate only.  It does not price a swap, construct calldata,
    or confer execution permission.
    """
    inventory = json.loads(config.inventory.read_text(encoding="utf-8"))
    if inventory.get("chain_id") != 57073:
        raise ValueError("inventory is not an Ink (57073) snapshot")
    policy = load_policy(config.policy)
    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for venue, pools, pool_policy in (
        ("velodrome_v2", inventory.get("velodrome_v2", []), policy["velodrome_v2_pools"]),
        ("slipstream", inventory.get("slipstream", []), policy["slipstream_pools"]),
    ):
        if not isinstance(pools, list):
            raise ValueError(f"inventory {venue} must be a list")
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            address = str(pool.get("address", "")).lower()
            token0 = str(pool.get("token0", "")).lower()
            token1 = str(pool.get("token1", "")).lower()
            if address not in pool_policy:
                rejected.append(_rejection(pool, "pool_not_allowlisted", venue))
            elif token0 not in policy["tokens"] or token1 not in policy["tokens"]:
                rejected.append(_rejection(pool, "token_not_allowlisted", venue))
            elif venue == "velodrome_v2" and not pool.get("has_nonzero_reserves"):
                rejected.append(_rejection(pool, "zero_reserves", venue))
            elif venue == "velodrome_v2" and not pool.get("recently_updated"):
                rejected.append(_rejection(pool, "stale_reserves", venue))
            elif venue == "slipstream" and not pool.get("has_positive_liquidity"):
                rejected.append(_rejection(pool, "zero_liquidity", venue))
            else:
                candidates.append({"venue": venue, **pool})
    result = {
        "schema_version": 1,
        "chain_id": 57073,
        "inventory_block_number": inventory["block_number"],
        "inventory_block_timestamp": inventory["block_timestamp"],
        "policy_path": str(config.policy),
        "generated_at_unix": time.time(),
        "candidates": candidates,
        "rejected": rejected,
        "summary": {"candidates": len(candidates), "rejected": len(rejected)},
    }
    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
