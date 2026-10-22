"""Checkpointed, read-only discovery of Ink PoolManager v4 pools.

PoolManager deliberately does not enumerate pool keys.  The canonical discovery
source is therefore its ``Initialize`` event.  Ink's Blockscout v2 API provides
decoded, paginated logs; this module records the exact source block/log index
and persists the next page cursor after every page.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp


INK_POOL_MANAGER = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
# Blockscout accepts the event signature in ``topic``.  Restricting the crawl
# at the source makes a complete Initialize inventory tractable; topic0 is not
# the API's parameter name on Ink.
INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
BLOCKSCOUT_LOGS = f"https://explorer.inkonchain.com/api/v2/addresses/{INK_POOL_MANAGER}/logs?topic={INITIALIZE_TOPIC}"


@dataclass(frozen=True)
class V4DiscoveryConfig:
    output: Path = Path("data/ink-v4-pools.json")
    endpoint: str = BLOCKSCOUT_LOGS
    max_pages: int = 10
    restart: bool = False
    requests_per_second: float = 1.0


def _event_name(event: dict[str, Any]) -> str | None:
    decoded = event.get("decoded")
    if not isinstance(decoded, dict):
        return None
    abi = decoded.get("abi")
    return abi.get("name") if isinstance(abi, dict) else None


def _parameters(event: dict[str, Any]) -> dict[str, str]:
    decoded = event.get("decoded")
    if not isinstance(decoded, dict):
        return {}
    parameters = decoded.get("parameters")
    if not isinstance(parameters, list):
        return {}
    return {
        item["name"]: str(item["value"])
        for item in parameters
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("value") is not None
    }


def parse_initialize(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return a normalized Initialize event, rejecting incomplete decoded logs."""
    if _event_name(event) != "Initialize":
        return None
    values = _parameters(event)
    required = {"id", "currency0", "currency1", "fee", "tickSpacing", "hooks", "sqrtPriceX96", "tick"}
    if not required.issubset(values):
        return None
    try:
        return {
            "pool_id": values["id"].lower(),
            "currency0": values["currency0"].lower(),
            "currency1": values["currency1"].lower(),
            "fee_pips": int(values["fee"]),
            "tick_spacing": int(values["tickSpacing"]),
            "hooks": values["hooks"].lower(),
            "sqrt_price_x96": str(int(values["sqrtPriceX96"])),
            "tick": int(values["tick"]),
            "source": {
                "block_number": int(event["block_number"]),
                "block_hash": event["block_hash"],
                "log_index": int(event["index"]),
                "transaction_hash": event["transaction_hash"],
                "block_timestamp": event["block_timestamp"],
            },
        }
    except (KeyError, TypeError, ValueError):
        return None


def _fresh_state(config: V4DiscoveryConfig) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "chain_id": 57073,
        "pool_manager": INK_POOL_MANAGER,
        "endpoint": config.endpoint,
        "event_topic": INITIALIZE_TOPIC,
        "next_page_params": {},
        "complete": False,
        "pages_scanned": 0,
        "events_seen": 0,
        "invalid_initialize_events": 0,
        "pools": {},
        "updated_at_unix": time.time(),
    }


def _load_state(config: V4DiscoveryConfig) -> dict[str, Any]:
    if config.restart or not config.output.exists():
        return _fresh_state(config)
    state = json.loads(config.output.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1 or state.get("chain_id") != 57073:
        raise ValueError("checkpoint has an incompatible schema or chain")
    if state.get("pool_manager", "").lower() != INK_POOL_MANAGER:
        raise ValueError("checkpoint belongs to a different PoolManager")
    if state.get("endpoint") != config.endpoint:
        raise ValueError("checkpoint endpoint differs; use --restart to change it")
    if not isinstance(state.get("pools"), dict):
        raise ValueError("checkpoint pools must be a mapping")
    if not isinstance(state.get("next_page_params"), dict):
        raise ValueError("checkpoint cursor must be a mapping")
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at_unix"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _get_page(session: aiohttp.ClientSession, endpoint: str, cursor: dict[str, Any]) -> dict[str, Any]:
    try:
        async with session.get(endpoint, params=cursor) as response:
            if response.status != 200:
                raise RuntimeError(f"Blockscout HTTP {response.status}")
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(f"Blockscout transport error: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("Blockscout returned an invalid logs page")
    return payload


async def discover_v4_pools(config: V4DiscoveryConfig) -> dict[str, Any]:
    """Advance a paginated Initialize crawl and persist every completed page."""
    if config.max_pages < 1:
        raise ValueError("max_pages must be positive")
    if config.requests_per_second <= 0:
        raise ValueError("requests_per_second must be positive")
    state = _load_state(config)
    if state["complete"]:
        return state
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        for page_number in range(config.max_pages):
            page = await _get_page(session, config.endpoint, state["next_page_params"])
            for event in page["items"]:
                if not isinstance(event, dict):
                    continue
                state["events_seen"] += 1
                if _event_name(event) != "Initialize":
                    continue
                initialized = parse_initialize(event)
                if initialized is None:
                    state["invalid_initialize_events"] += 1
                    continue
                # A pool ID can occur only once; retaining the canonical first
                # occurrence means an explorer duplicate cannot rewrite evidence.
                state["pools"].setdefault(initialized["pool_id"], initialized)
            state["pages_scanned"] += 1
            next_cursor = page.get("next_page_params")
            state["next_page_params"] = next_cursor if isinstance(next_cursor, dict) else {}
            state["complete"] = not bool(next_cursor)
            _write_state(config.output, state)
            if state["complete"] or page_number + 1 == config.max_pages:
                break
            await asyncio.sleep(1 / config.requests_per_second)
    return state
