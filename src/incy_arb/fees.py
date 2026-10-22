"""Pinned OP Stack fee budgets; no signing or transaction submission."""
from __future__ import annotations

from typing import Any

import rlp
from eth_utils import is_address

from .abi import decode_result, encode_call, hex_bytes
from .flash_credit import NATIVE_CURRENCY
from .rpc import ReadOnlyRpc, RpcError

GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000f"
WETH = "0x4200000000000000000000000000000000000006"


def unsigned_envelope(calldata: bytes, target: str, gas_limit: int, max_fee: int,
                      priority_fee: int = 0, nonce: int = 0) -> bytes:
    if not is_address(target) or int(target, 16) == 0:
        raise ValueError("a nonzero transaction destination is required")
    if not 0 < gas_limit < 1 << 64 or not 0 <= nonce < 1 << 64:
        raise ValueError("gas limit and nonce are outside transaction bounds")
    if not 0 <= priority_fee <= max_fee < 1 << 256:
        raise ValueError("fee caps must satisfy 0 <= priority <= maximum < 2^256")
    # Type-2 transaction, zero ETH value, empty access list, no signature fields.
    return b"\x02" + rlp.encode([57073, nonce, priority_fee, max_fee, gas_limit,
                                bytes.fromhex(target[2:]), 0, calldata, []])


async def fee_budget(rpc: ReadOnlyRpc, calldata: bytes, target: str, block: int, gas_limit: int,
                     *, max_fee: int | None = None, priority_fee: int = 0, nonce: int = 0) -> dict[str, Any]:
    if await rpc.chain_id() != 57073:
        raise ValueError("fee budget requires Ink chain ID 57073")
    before = await rpc.sealed_block(block)
    if not before or not before.get("hash") or "baseFeePerGas" not in before:
        raise RuntimeError("pinned block hash/base fee is unavailable")
    base_fee = int(before["baseFeePerGas"], 16)
    maximum = 2 * base_fee + priority_fee if max_fee is None else max_fee
    if maximum < base_fee + priority_fee:
        raise ValueError("max fee cannot cover the pinned base fee plus requested priority")
    envelope = unsigned_envelope(calldata, target, gas_limit, maximum, priority_fee, nonce)

    async def read(signature: str, value: int) -> int:
        raw = await rpc.contract_call(GAS_PRICE_ORACLE, encode_call(signature, ["uint256"], [value]), block)
        return decode_result(["uint256"], hex_bytes(raw))[0]

    # The deployed oracle adds its own 68-byte signature allowance. Do not add it twice.
    l1_fee = await read("getL1FeeUpperBound(uint256)", len(envelope))
    operator_fee = await read("getOperatorFee(uint256)", gas_limit)
    try:
        raw = await rpc.contract_call(GAS_PRICE_ORACLE, encode_call("getL1Fee(bytes)", ["bytes"], [envelope]), block)
        content_l1 = decode_result(["uint256"], hex_bytes(raw))[0]
        content_estimate = {
            "status": "estimated", "method": "getL1Fee(bytes)",
            "l1_fee_estimate": str(content_l1),
            "total_fee_estimate_wei": str(gas_limit * maximum + content_l1 + operator_fee),
            "unsigned_transaction": "0x" + envelope.hex(),
            "warning": "Compression estimate for this exact unsigned destination/nonce/calldata. The oracle adds its own signature allowance. Hypothetical executor address and future inclusion fees can differ.",
        }
    except RpcError as error:
        content_estimate = {"status": "unavailable", "error": str(error), "rpc_code": error.code}
    after = await rpc.sealed_block(block)
    if not after or after.get("hash") != before["hash"]:
        raise RuntimeError("fee block changed during reads; discard budget")
    return {
        "chain_id": 57073, "status": "estimated", "block_number": block, "block_hash": before["hash"],
        "gas_price_oracle": GAS_PRICE_ORACLE, "transaction_type": 2, "destination": target,
        "nonce": nonce, "unsigned_transaction_size": len(envelope), "gas_limit": gas_limit,
        "base_fee_per_gas": str(base_fee), "max_fee_per_gas": str(maximum),
        "max_priority_fee_per_gas": str(priority_fee), "l2_fee_at_maximum": str(gas_limit * maximum),
        "l1_fee_upper_estimate": str(l1_fee), "operator_fee_estimate": str(operator_fee),
        "total_fee_budget_wei": str(gas_limit * maximum + l1_fee + operator_fee),
        "content_based_estimate": content_estimate,
        "warning": "Pinned fee estimate, not an inclusion guarantee. Fjord compression bound is statistical; future fees may change.",
    }


def assess_net_profit(simulation: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    if simulation.get("status") != "success":
        return {"status": "execution_not_confirmed"}
    if not simulation.get("gas_estimate"):
        return {"status": "gas_estimate_unavailable"}
    if budget.get("status") != "estimated":
        return {"status": "fee_budget_unavailable"}
    if simulation["block_hash"] != budget["block_hash"] or simulation["block_number"] != budget["block_number"]:
        raise ValueError("fee budget and simulation must refer to the same sealed block")
    if simulation["start_token"].lower() not in (WETH, NATIVE_CURRENCY):
        return {"status": "start_token_valuation_required", "start_token": simulation["start_token"]}
    net = int(simulation["simulated_gross_pnl"]) - int(budget["total_fee_budget_wei"])
    result: dict[str, Any] = {"status": "estimated_positive" if net > 0 else "not_positive_after_fee_budget",
            "estimated_net_pnl_native_wei": str(net),
            "warning": "Native ETH/WETH valued in ETH wei. Estimate excludes deployment, capital provisioning, and payout conversion."}
    content = budget.get("content_based_estimate", {})
    if content.get("status") == "estimated":
        content_net = int(simulation["simulated_gross_pnl"]) - int(content["total_fee_estimate_wei"])
        result["content_based_estimate"] = {
            "status": "estimated_positive" if content_net > 0 else "not_positive_after_content_estimate",
            "estimated_net_pnl_native_wei": str(content_net),
            "warning": "Supplemental compression estimate only; does not change the upper-budget assessment or prove live profitability.",
        }
    else:
        result["content_based_estimate"] = {"status": "unavailable"}
    return result
