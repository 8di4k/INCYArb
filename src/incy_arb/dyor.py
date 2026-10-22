"""Runtime-scoped DyorSwap fee recognition for read-only research."""

from __future__ import annotations

from typing import Any

from eth_utils import keccak

from .rpc import ReadOnlyRpc

DYORSWAP_FACTORY = "0x6c86ab200661512fdbd27da4bb87df15609a2806"
# Swap invariant: balance * 10000 - amountIn * 30, runtime PC 0xda5..0xe40.
# Independent full-route parity and understated-fee controls are archived in docs/validation.
DYOR_30BPS_RUNTIME_HASH = "0xe21efb434c9e1a20cdd4d9df255bcade55c20e175c5d23b27f7fc3290ca96ef1"


async def dyor_fee_metadata(rpc: ReadOnlyRpc, address: str, block: int) -> dict[str, Any]:
    code = await rpc.code_at(address, block)
    digest = "0x" + keccak(bytes.fromhex(code.removeprefix("0x"))).hex()
    recognized = digest == DYOR_30BPS_RUNTIME_HASH
    return {
        "runtime_code_hash": digest,
        "fee_bps": 30 if recognized else None,
        "fee_model": "recognized_dyor_30bps_runtime" if recognized else "unknown_dyor_runtime",
    }
