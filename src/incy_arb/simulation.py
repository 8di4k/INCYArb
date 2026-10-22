"""Full-route eth_call simulation. No keys, signing, deployment, or send methods."""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any

from eth_abi import encode
from eth_abi.exceptions import DecodingError
from eth_utils import is_address, keccak

from .abi import decode_result, encode_call, function_selector, hex_bytes
from .flash_credit import INT128_MAX, POOL_MANAGER
from .rpc import ReadOnlyRpc, RpcError

SIM_ADDRESS = "0x00000000000000000000000000000000000a4b17"
SIM_CALLER = "0x00000000000000000000000000000000000a4b18"
GAS_LIMIT = 12_000_000


def _address(value: str) -> bytes:
    if not is_address(value) or int(value, 16) == 0:
        raise ValueError("simulation requires nonzero ERC-20/pool addresses")
    return bytes.fromhex(value[2:])


def encode_simulation_route(route: dict[str, Any], block: int, min_profit: int = 0) -> bytes:
    """Encode only validated V2, Slipstream, and zero-hook static-fee v4 steps."""
    token = route["start_token"].lower()
    amount = int(route["amount_in"])
    if not 0 < amount <= INT128_MAX or not 0 <= min_profit <= INT128_MAX or not 0 <= block < 1 << 32:
        raise ValueError("route amount/profit/block exceeds packed accounting bounds")
    steps = route["steps"]
    if not 2 <= len(steps) <= 8:
        raise ValueError("a simulation route must contain 2–8 steps")
    packed = bytearray(function_selector("execute()") + _address(token) + amount.to_bytes(16, "big")
                       + min_profit.to_bytes(16, "big") + block.to_bytes(4, "big"))
    current = token
    used: set[str] = set()
    for step in steps:
        pool, venue = step["pool"], step["venue"]
        output = step["token_out"].lower()
        if step["token_in"].lower() != current or current == output:
            raise ValueError("route token continuity is invalid")
        if {current, output} != {pool["token0"].lower(), pool["token1"].lower()}:
            raise ValueError("route tokens do not match pool sides")
        identity = pool["address"].lower()
        if identity in used:
            raise ValueError("repeated pool in simple cycle")
        used.add(identity)
        if venue == "slipstream":
            packed += b"\x01" + _address(identity) + _address(output)
        elif venue in ("uniswap_v2", "velodrome_v2"):
            if pool.get("stable"):
                raise ValueError("stable Velodrome V2 needs a separate execution adapter")
            fee = int(pool["fee_bps"])
            if not 0 <= fee < 10_000:
                raise ValueError("invalid V2 fee")
            packed += b"\x04" + _address(identity) + _address(output) + fee.to_bytes(2, "big")
        elif venue == "uniswap_v4":
            fee, spacing = int(pool["fee_pips"]), int(pool["tick_spacing"])
            if int(pool["hooks"], 16) != 0 or not 0 <= fee <= 1_000_000 or not 0 < spacing < 1 << 23:
                raise ValueError("v4 simulation accepts only static fees, positive spacing, and zero hooks")
            key = (min(current, output), max(current, output), fee, spacing, pool["hooks"])
            pool_id = "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [key])).hex()
            if identity != pool_id:
                raise ValueError("v4 pool ID does not match its key")
            packed += b"\x02" + _address(output) + fee.to_bytes(3, "big") + spacing.to_bytes(3, "big") + bytes(20)
        else:
            raise ValueError(f"unsupported simulation venue: {venue}")
        current = output
    if current != token:
        raise ValueError("route must return to its start token")
    return bytes(packed)


def _word(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def simulator_overrides(runtime: str) -> dict[str, Any]:
    executor_slot = "0x" + keccak(encode(["address", "uint256"], [SIM_CALLER, 2])).hex()
    return {
        SIM_ADDRESS: {"code": runtime, "stateDiff": {
            _word(0): _word(int(SIM_CALLER, 16)), _word(1): _word(int(SIM_CALLER, 16)), executor_slot: _word(1),
        }},
        SIM_CALLER: {"balance": hex(10**21)},
    }


def decode_simulation_error(error: RpcError) -> dict[str, Any]:
    data = error.data
    if isinstance(data, dict):
        data = data.get("data", data.get("result"))
    result = {"message": str(error), "rpc_code": error.code, "revert_data": data}
    if not isinstance(data, str) or not data.startswith("0x"):
        return result
    for signature, types in (
        ("ProfitTooLow(uint256,uint256)", ["uint256", "uint256"]),
        ("NegativeProfit(int256)", ["int256"]),
        ("OverPay(uint256,uint256)", ["uint256", "uint256"]),
        ("TransferFailed(address)", ["address"]), ("Error(string)", ["string"]),
        ("Panic(uint256)", ["uint256"]), ("UnknownKind(uint8)", ["uint8"]),
        *((name + "()", []) for name in ("BadCallback", "CallbackMissing", "BadRoute", "CurrencyNotSettled",
                                         "NotExecutor", "IsPaused", "Expired", "Reentry", "Unauthorized")),
    ):
        if data[2:10].lower() == function_selector(signature).hex():
            result["error"] = signature.split("(")[0]
            try:
                result["arguments"] = list(decode_result(types, hex_bytes(data)[4:])) if types else []
            except (ValueError, OverflowError, DecodingError):
                result["malformed_error_data"] = True
            break
    return result


async def simulate_finding(rpc: ReadOnlyRpc, scan: dict[str, Any], index: int = 0) -> dict[str, Any]:
    if scan.get("chain_id") != 57073 or await rpc.chain_id() != 57073:
        raise ValueError("simulation requires Ink scan and RPC chain ID 57073")
    if not 0 <= index < len(scan["findings"]):
        raise ValueError("finding index is out of range")
    finding = scan["findings"][index]
    block = int(scan["quote_block_number"])
    data = encode_simulation_route(finding["simulation_route"], block)
    before = await rpc.sealed_block(block)
    if not before or not before.get("hash"):
        raise RuntimeError("could not pin simulation block hash")
    if await rpc.code_at(SIM_ADDRESS, block) != "0x" or await rpc.code_at(SIM_CALLER, block) != "0x":
        raise RuntimeError("simulation placeholder addresses are occupied")
    artifact = json.loads(files("incy_arb").joinpath("artifacts/InkArb.json").read_text())
    creation = hex_bytes(artifact["creation_code"])
    constructor = encode(["address", "address", "address"], [POOL_MANAGER, SIM_CALLER, SIM_CALLER])
    runtime = await rpc.simulation_call(
        {"from": SIM_CALLER, "data": "0x" + (creation + constructor).hex(), "gas": hex(GAS_LIMIT)}, block,
        {SIM_CALLER: {"balance": hex(10**21)}},
    )
    if not isinstance(runtime, str) or len(hex_bytes(runtime)) < 100:
        raise RuntimeError("constructor simulation did not return valid runtime code")
    overrides = simulator_overrides(runtime)
    # Check the injected immutable and mapping layout against the actual compiled getters.
    for call, expected in ((encode_call("poolManager()"), int(POOL_MANAGER, 16)),
                           (encode_call("isExecutor(address)", ["address"], [SIM_CALLER]), 1),
                           (encode_call("beneficiary()"), int(SIM_CALLER, 16))):
        answer = await rpc.simulation_call({"from": SIM_CALLER, "to": SIM_ADDRESS, "data": "0x" + call.hex()}, block, overrides)
        if int(answer, 16) != expected:
            raise RuntimeError("simulator immutable/storage verification failed")
    transaction = {"from": SIM_CALLER, "to": SIM_ADDRESS, "data": "0x" + data.hex(), "gas": hex(GAS_LIMIT)}
    result: dict[str, Any] = {
        "chain_id": 57073, "source": "sealed_eth_call_with_simulator_code_override", "block_number": block,
        "block_hash": before["hash"], "finding_index": index,
        "quoted_gross_pnl": finding["gross_pnl"], "calldata": transaction["data"],
        "creation_sha256": hashlib.sha256(creation).hexdigest(),
        "runtime_keccak256": "0x" + keccak(hex_bytes(runtime)).hex(),
        "overridden_accounts": [SIM_ADDRESS, SIM_CALLER], "market_state_overridden": False,
        "warning": "Hypothetical executor only. No deployment, execution permission, or net-profit claim.",
    }
    try:
        returned = await rpc.simulation_call(transaction, block, overrides)
        profit = decode_result(["uint256"], hex_bytes(returned))[0]
        result.update(status="success", simulated_gross_pnl=str(profit),
                      quote_difference=str(profit - int(finding["gross_pnl"])))
    except RpcError as error:
        result.update(status="execution_reverted" if error.code == 3 or error.data else "rpc_error",
                      execution_error=decode_simulation_error(error))
        decoded = result["execution_error"]
        if decoded.get("error") == "NegativeProfit" and "arguments" in decoded:
            loss = decoded["arguments"][0]
            result.update(simulated_gross_pnl=str(loss), quote_difference=str(loss - int(finding["gross_pnl"])))
    if result["status"] == "success":
        try:
            result["gas_estimate"] = await rpc.simulation_gas(transaction, block, overrides)
        except RpcError as error:
            result["gas_estimate"] = None
            result["gas_error"] = decode_simulation_error(error)
    after = await rpc.sealed_block(block)
    if not after or after.get("hash") != before["hash"]:
        raise RuntimeError("simulation block changed during reads; discard result")
    return result


async def simulate_scan(scan: dict[str, Any], rpc_url: str, index: int = 0) -> dict[str, Any]:
    async with ReadOnlyRpc(rpc_url) as rpc:
        return await simulate_finding(rpc, scan, index)
