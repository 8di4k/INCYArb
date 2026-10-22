"""Strict assembly and sealed-block verification for Ink subblocks.

The stream is pre-confirmed data. A connection may begin in the middle of a
payload, so no payload is emitted until its own index 0 and base are observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eth_utils import keccak


def _hex_int(value: str | int) -> int:
    return int(value, 16) if isinstance(value, str) else value


def _tx_kind(raw: bytes) -> str:
    if not raw:
        return "empty"
    if raw[0] >= 0xC0:
        return "legacy"
    return {
        0x01: "eip2930",
        0x02: "eip1559",
        0x04: "eip7702",
        0x7E: "deposit",
    }.get(raw[0], f"typed_0x{raw[0]:02x}")


@dataclass(frozen=True)
class CapturedTx:
    raw: str
    hash: str
    kind: str
    receipt: dict[str, Any]


@dataclass
class Payload:
    payload_id: str
    block_number: int
    base: dict[str, Any]
    messages: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[CapturedTx] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message_timings: list[dict[str, Any]] = field(default_factory=list)

    def add(self, message: dict[str, Any]) -> None:
        index = message.get("index")
        metadata = message.get("metadata")
        diff = message.get("diff")
        if not isinstance(index, int) or not isinstance(metadata, dict) or not isinstance(diff, dict):
            self.errors.append("malformed message")
            return
        if message.get("payload_id") != self.payload_id:
            self.errors.append("payload id changed within payload")
            return
        if _hex_int(metadata.get("block_number", -1)) != self.block_number:
            self.errors.append("block number changed within payload")
        if index in self.indices:
            self.errors.append(f"duplicate index {index}")
        self.indices.append(index)
        self.messages.append(message)

        receipts = metadata.get("receipts")
        raw_transactions = diff.get("transactions")
        if not isinstance(receipts, dict) or not isinstance(raw_transactions, list):
            self.errors.append(f"invalid receipt or transaction collection at index {index}")
            return
        matched_receipts: set[str] = set()
        for encoded in raw_transactions:
            if not isinstance(encoded, str) or not encoded.startswith("0x"):
                self.errors.append(f"invalid raw transaction at index {index}")
                continue
            try:
                raw = bytes.fromhex(encoded[2:])
            except ValueError:
                self.errors.append(f"non-hex raw transaction at index {index}")
                continue
            tx_hash = "0x" + keccak(raw).hex()
            matched_receipts.add(tx_hash)
            receipt = receipts.get(tx_hash)
            if not isinstance(receipt, dict):
                self.errors.append(f"missing receipt for {tx_hash}")
                continue
            if "status" not in receipt:
                self.errors.append(f"receipt without status for {tx_hash}")
            self.transactions.append(CapturedTx(encoded.lower(), tx_hash, _tx_kind(raw), receipt))
        if set(receipts) - matched_receipts:
            self.errors.append(f"extra receipts without raw transactions at index {index}")

    @property
    def valid(self) -> bool:
        return not self.errors and self.indices == list(range(len(self.indices)))

    def record(self) -> dict[str, Any]:
        return {
            "payload_id": self.payload_id,
            "block_number": self.block_number,
            "base": self.base,
            "indices": self.indices,
            "errors": self.errors,
            "valid": self.valid,
            "transaction_hashes": [tx.hash for tx in self.transactions],
            "transaction_kinds": [tx.kind for tx in self.transactions],
            # Preserve all raw stream data for deterministic later replay.
            "messages": self.messages,
            "message_timings": self.message_timings,
        }


class PayloadCollector:
    """Turns a stream into complete payloads without assuming a fixed last index."""

    def __init__(self) -> None:
        self._active: Payload | None = None
        self.ignored_partial_messages = 0

    @property
    def active_payload(self) -> Payload | None:
        return self._active

    def add(self, message: dict[str, Any]) -> list[Payload]:
        index = message.get("index")
        payload_id = message.get("payload_id")
        metadata = message.get("metadata")
        if not isinstance(index, int) or not isinstance(payload_id, str) or not isinstance(metadata, dict):
            self.ignored_partial_messages += 1
            return []

        if self._active is None:
            if index != 0 or not isinstance(message.get("base"), dict):
                self.ignored_partial_messages += 1
                return []
            self._active = self._start(message)
            return []

        if payload_id == self._active.payload_id:
            self._active.add(message)
            return []

        finished = self._active
        if index != 0 or not isinstance(message.get("base"), dict):
            finished.errors.append("stream switched payload before next base")
            self._active = None
            self.ignored_partial_messages += 1
            return [finished]

        self._active = self._start(message)
        return [finished]

    @staticmethod
    def _start(message: dict[str, Any]) -> Payload:
        metadata = message["metadata"]
        payload = Payload(message["payload_id"], _hex_int(metadata["block_number"]), message["base"])
        base_number = _hex_int(message["base"].get("block_number", -1))
        if base_number != payload.block_number:
            payload.errors.append("base and metadata block number differ")
        payload.add(message)
        return payload


def verify_sealed(payload: Payload, sealed: dict[str, Any] | None) -> dict[str, Any]:
    """Compare an assembled payload with a sealed RPC block without trusting stream roots."""
    result: dict[str, Any] = {"verified": False, "differences": []}
    if sealed is None:
        result["differences"].append("sealed block unavailable")
        return result
    try:
        if _hex_int(sealed["number"]) != payload.block_number:
            result["differences"].append("sealed block number differs")
        expected = [tx.hash for tx in payload.transactions]
        actual = sealed.get("transactions")
        if not isinstance(actual, list) or not all(isinstance(x, str) for x in actual):
            result["differences"].append("sealed transactions are not hash list")
        elif expected != [x.lower() for x in actual]:
            result["differences"].append("sealed transaction order or set differs")
        if payload.base.get("parent_hash", "").lower() != sealed.get("parentHash", "").lower():
            result["differences"].append("parent hash differs")
        if _hex_int(payload.base.get("timestamp", -1)) != _hex_int(sealed.get("timestamp", -2)):
            result["differences"].append("timestamp differs")
        if _hex_int(payload.base.get("base_fee_per_gas", -1)) != _hex_int(sealed.get("baseFeePerGas", -2)):
            result["differences"].append("base fee differs")
    except (KeyError, TypeError, ValueError):
        result["differences"].append("malformed sealed block")
    result["verified"] = payload.valid and not result["differences"]
    return result


def _log_contents(log: dict[str, Any]) -> tuple[bytes, tuple[bytes, ...], bytes]:
    if not isinstance(log, dict) or log.get("removed", False):
        raise ValueError("invalid or removed log")
    address, topics, data = log["address"], log["topics"], log["data"]
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError("invalid log address")
    if not isinstance(topics, list) or len(topics) > 4:
        raise ValueError("invalid log topics")
    if not all(isinstance(topic, str) and len(topic) == 66 and topic.startswith("0x") for topic in topics):
        raise ValueError("invalid topic width")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("invalid log data")
    return bytes.fromhex(address[2:]), tuple(bytes.fromhex(topic[2:]) for topic in topics), bytes.fromhex(data[2:])


def verify_receipts(payload: Payload, receipts: list[dict[str, Any]], block_hash: str) -> dict[str, Any]:
    """Compare replay-relevant receipt fields and ordered logs with sealed RPC receipts."""
    result: dict[str, Any] = {
        "requested": True,
        "verified": False,
        "block_hash": block_hash,
        "checked_transactions": 0,
        "checked_logs": 0,
        "differences": [],
        "fields": ["status", "type", "cumulativeGasUsed", "ordered_logs"],
    }
    differences = result["differences"]
    if not payload.valid:
        differences.append("payload is structurally invalid")
        return result
    try:
        if not isinstance(receipts, list) or any(not isinstance(receipt, dict) for receipt in receipts):
            raise ValueError("invalid receipt collection")
        by_hash = {receipt["transactionHash"].lower(): receipt for receipt in receipts}
        expected_hashes = {tx.hash for tx in payload.transactions}
        if len(by_hash) != len(receipts) or set(by_hash) != expected_hashes:
            differences.append("sealed receipt transaction set differs")
            return result
        for index, tx in enumerate(payload.transactions):
            actual = by_hash[tx.hash]
            if (
                _hex_int(actual["blockNumber"]) != payload.block_number
                or actual["blockHash"].lower() != block_hash.lower()
                or _hex_int(actual["transactionIndex"]) != index
            ):
                differences.append(f"receipt block or index differs for {tx.hash}")
            for field_name in result["fields"][:-1]:
                if _hex_int(tx.receipt[field_name]) != _hex_int(actual[field_name]):
                    differences.append(f"receipt {field_name} differs for {tx.hash}")
            stream_logs, sealed_logs = tx.receipt["logs"], actual["logs"]
            status = _hex_int(actual["status"])
            if status not in (0, 1) or (status == 0 and sealed_logs):
                raise ValueError("impossible failed-transaction log evidence")
            if not isinstance(stream_logs, list) or not isinstance(sealed_logs, list):
                raise ValueError("invalid logs collection")
            if [_log_contents(log) for log in stream_logs] != [_log_contents(log) for log in sealed_logs]:
                differences.append(f"receipt logs differ for {tx.hash}")
            result["checked_transactions"] += 1
            result["checked_logs"] += len(sealed_logs)
    except (KeyError, TypeError, ValueError, AttributeError):
        differences.append("malformed stream or sealed receipts")
    result["verified"] = not differences
    return result
