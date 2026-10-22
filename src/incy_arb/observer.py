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
from .timing import payload_timing
from .subblocks import Payload, PayloadCollector, verify_receipts, verify_sealed

LOG = logging.getLogger(__name__)
INK_SUBBLOCKS = "wss://subblocks.inkonchain.com/ws"
INK_RPC = "https://rpc-gel.inkonchain.com"


@dataclass(frozen=True)
class ObserverConfig:
    websocket_url: str = INK_SUBBLOCKS
    rpc_url: str = INK_RPC
    duration_seconds: float = 60.0
    output: Path = Path("data/ink-subblocks.jsonl")
    verify_receipt_logs: bool = True


async def observe(config: ObserverConfig) -> dict[str, int]:
    """Capture complete payloads until duration expires; duration 0 means forever."""
    config.output.parent.mkdir(parents=True, exist_ok=True)
    collector = PayloadCollector()
    finished: set[asyncio.Task[dict[str, Any]]] = set()
    verify_slots = asyncio.Semaphore(2)
    captured = verified = invalid = receipts_verified = 0
    start = time.monotonic()

    async with ReadOnlyRpc(config.rpc_url) as rpc:
        with config.output.open("a", encoding="utf-8") as output:
            async with websockets.connect(config.websocket_url, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                LOG.info("connected to Ink subblocks")
                while config.duration_seconds == 0 or time.monotonic() - start < config.duration_seconds:
                    remaining = (
                        None
                        if config.duration_seconds == 0
                        else max(0.1, config.duration_seconds - (time.monotonic() - start))
                    )
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        received_monotonic, received_at_unix = time.monotonic(), time.time()
                    except TimeoutError:
                        break
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        LOG.warning("discarded malformed JSON stream message")
                        continue
                    completed = collector.add(message)
                    active = collector.active_payload
                    if active and active.messages and active.messages[-1] is message:
                        active.message_timings.append({"index": message["index"],
                                                       "received_monotonic": received_monotonic,
                                                       "received_at_unix": received_at_unix})
                    for payload in completed:
                        finished.add(
                            asyncio.create_task(
                                _verify_and_record(payload, rpc, output, verify_slots, config.verify_receipt_logs)
                            )
                        )
                    done = {task for task in finished if task.done()}
                    for task in done:
                        record = task.result()
                        captured += 1
                        verified += int(record["sealed_verification"]["verified"])
                        invalid += int(not record["valid"])
                        receipts_verified += int(record["receipt_verification"]["verified"])
                    finished -= done

            for task in asyncio.as_completed(finished):
                record = await task
                captured += 1
                verified += int(record["sealed_verification"]["verified"])
                invalid += int(not record["valid"])
                receipts_verified += int(record["receipt_verification"]["verified"])

    return {
        "captured": captured,
        "receipts_verified": receipts_verified,
        "verified": verified,
        "invalid": invalid,
        "ignored_partial_messages": collector.ignored_partial_messages,
    }


async def _verify_and_record(
    payload: Payload,
    rpc: ReadOnlyRpc,
    output: Any,
    verify_slots: asyncio.Semaphore,
    verify_receipt_logs: bool = True,
) -> dict[str, Any]:
    queued, started = time.monotonic(), None
    receipt_verification: dict[str, Any] = {"requested": verify_receipt_logs, "verified": False, "differences": []}
    try:
        async with verify_slots:
            started = time.monotonic()
            sealed = await rpc.sealed_block(payload.block_number)
            verification = verify_sealed(payload, sealed)
            verification["transaction_order_verified"] = verification["verified"]
            if verify_receipt_logs and verification["verified"]:
                if not sealed or not sealed.get("hash"):
                    raise RpcError("sealed block hash unavailable for receipt verification")
                receipts = await rpc.block_receipts(payload.block_number)
                receipt_verification = verify_receipts(payload, receipts, sealed["hash"])
                after = await rpc.sealed_block(payload.block_number)
                if not after or after.get("hash") != sealed["hash"]:
                    receipt_verification["verified"] = False
                    receipt_verification["differences"].append("sealed block changed during receipt reads")
                verification["verified"] = receipt_verification["verified"]
                if not receipt_verification["verified"]:
                    verification["differences"].append("sealed receipt verification failed")
    except RpcError as exc:
        verification = {"verified": False, "differences": [f"RPC error: {exc}"]}
        if verify_receipt_logs:
            receipt_verification["differences"].append(f"RPC error: {exc}")
    record = payload.record()
    record["timing"] = payload_timing(payload, queued, started, time.monotonic())
    record["captured_at_unix"] = time.time()
    record["sealed_verification"] = verification
    record["receipt_verification"] = receipt_verification
    output.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    output.flush()
    return record
