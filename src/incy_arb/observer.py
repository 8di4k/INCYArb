"""Read-only Ink subblock capture and sealed verification."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from .rpc import ReadOnlyRpc, RpcError
from .subblocks import Payload, PayloadCollector, verify_sealed

LOG = logging.getLogger(__name__)
INK_SUBBLOCKS = "wss://subblocks.inkonchain.com/ws"
INK_RPC = "https://rpc-gel.inkonchain.com"


@dataclass(frozen=True)
class ObserverConfig:
    websocket_url: str = INK_SUBBLOCKS
    rpc_url: str = INK_RPC
    duration_seconds: float = 60.0
    output: Path = Path("data/ink-subblocks.jsonl")


async def observe(config: ObserverConfig) -> dict[str, int]:
    """Capture complete payloads until duration expires; duration 0 means forever."""
    config.output.parent.mkdir(parents=True, exist_ok=True)
    collector = PayloadCollector()
    finished: set[asyncio.Task[dict[str, Any]]] = set()
    verify_slots = asyncio.Semaphore(2)
    captured = verified = invalid = 0
    start = time.monotonic()

    async with ReadOnlyRpc(config.rpc_url) as rpc:
        with config.output.open("a", encoding="utf-8") as output:
            async with websockets.connect(config.websocket_url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                LOG.info("connected to Ink subblocks")
                while config.duration_seconds == 0 or time.monotonic() - start < config.duration_seconds:
                    remaining = None if config.duration_seconds == 0 else max(0.1, config.duration_seconds - (time.monotonic() - start))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except TimeoutError:
                        break
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        LOG.warning("discarded malformed JSON stream message")
                        continue
                    for payload in collector.add(message):
                        finished.add(asyncio.create_task(_verify_and_record(payload, rpc, output, verify_slots)))
                    done = {task for task in finished if task.done()}
                    for task in done:
                        record = task.result()
                        captured += 1
                        verified += int(record["sealed_verification"]["verified"])
                        invalid += int(not record["valid"])
                    finished -= done

            for task in asyncio.as_completed(finished):
                record = await task
                captured += 1
                verified += int(record["sealed_verification"]["verified"])
                invalid += int(not record["valid"])

    return {
        "captured": captured,
        "verified": verified,
        "invalid": invalid,
        "ignored_partial_messages": collector.ignored_partial_messages,
    }


async def _verify_and_record(
    payload: Payload, rpc: ReadOnlyRpc, output: Any, verify_slots: asyncio.Semaphore
) -> dict[str, Any]:
    try:
        async with verify_slots:
            sealed = await rpc.sealed_block(payload.block_number)
            verification = verify_sealed(payload, sealed)
    except RpcError as exc:
        verification = {"verified": False, "differences": [f"RPC error: {exc}"]}
    record = payload.record()
    record["captured_at_unix"] = time.time()
    record["sealed_verification"] = verification
    output.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    output.flush()
    return record
