"""Minimal ABI helpers for read-only discovery calls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from eth_abi import decode, encode
from eth_utils import keccak

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"


def function_selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, types: Sequence[str] = (), values: Sequence[Any] = ()) -> bytes:
    return function_selector(signature) + encode(list(types), list(values))


def decode_result(types: Sequence[str], encoded: bytes) -> tuple[Any, ...]:
    return decode(list(types), encoded)


def hex_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("expected 0x-prefixed hex")
    return bytes.fromhex(value[2:])


@dataclass(frozen=True)
class ReadCall:
    target: str
    data: bytes
    label: str
    allow_failure: bool = False


def encode_aggregate3(calls: Sequence[ReadCall]) -> bytes:
    entries = [(call.target, call.allow_failure, call.data) for call in calls]
    return encode_call("aggregate3((address,bool,bytes)[])", ["(address,bool,bytes)[]"], [entries])


def decode_aggregate3(result: str) -> list[tuple[bool, bytes]]:
    return list(decode_result(["(bool,bytes)[]"], hex_bytes(result))[0])
