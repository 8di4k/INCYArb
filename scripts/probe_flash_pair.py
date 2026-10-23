"""Replay a bounded Base oracle pair with real v4 debt and unmodified market state.

Historical independent states, not a live strategy or a sequence of own fills.
The synthetic executor receives code; only the EOA gas balance is funded.
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import rlp
from eth_abi import decode
from eth_utils import keccak

from incy_arb.abi import encode_call, hex_bytes
from incy_arb.rpc import ReadOnlyRpc, RpcError

REPO = Path(__file__).resolve().parents[1]
SOURCE = Path("docs/validation/flash-funded-oracle/FlashPairProbe.sol")
EXECUTOR = "0x3ce862fe7e87ea2b6cd169db9166d7e297e70c11"
SENDER = "0x6326e53b96a3d5d0f2bda866e71280c626934f7c"
MAKER = "0x960f1e3181e928210c02a34ee29bf3d354dd2ff5"
MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ORACLE = "0x420000000000000000000000000000000000000f"
SIGNATURE = "probe((uint256,uint256,bool,address),uint256)"


def financed_result(raw, amount, rounds):
    profit, before, after, changes = decode(["uint256", "uint256", "uint256", "int256[]"], hex_bytes(raw))
    if before < amount or before != after or len(changes) != rounds or sum(changes) != profit:
        raise ValueError("loan principal or per-round changes do not reconcile")
    return {
        "response": raw,
        "profit_raw": str(profit),
        "manager_before": str(before),
        "manager_after": str(after),
        "changes": [str(v) for v in changes],
    }


def transaction_envelope(tx, gas_limit, base_fee, tip):
    if int(tx["gasPrice"], 16) != base_fee + tip or base_fee < 0 or tip < 0 or gas_limit <= 0:
        raise ValueError("transaction gas price must match the pinned base fee and tip")
    return b"\x02" + rlp.encode(
        [8453, 0, tip, base_fee + tip, gas_limit, bytes.fromhex(tx["to"][2:]), 0, hex_bytes(tx["data"]), []]
    )


def net_weth(row, header):
    """Conservative hypothetical execution budget, expressed entirely in ETH wei.

    Never extrapolates snapshots into repeatable income. Requires repayment,
    fees bound to the exact unsigned transaction, and 20% execution gas reserve.
    """
    if "profit_raw" not in row:
        return {"status": "execution_not_confirmed"}
    if row["debt_asset"].lower() != WETH:
        return {"status": "valuation_required"}
    if row["block"] != int(header["number"], 16) or row["block_hash"] != header["hash"]:
        raise ValueError("different canonical block")
    result = financed_result(row["response"], int(row["amount_raw"]), row["rounds"])
    if any(result[key] != row[key] for key in result):
        raise ValueError("decoded financed result was changed")
    limit = (row["gas_estimate"] * 120 + 99) // 100
    if row["gas_limit"] != limit:
        raise ValueError("20% gas reserve required")
    base = int(header["baseFeePerGas"], 16)
    tip = int(row["priority_fee_wei"])
    envelope = transaction_envelope(row["transaction"], limit, base, tip)
    if row["unsigned_transaction"] != "0x" + envelope.hex():
        raise ValueError("fee transaction differs from simulated calldata or caps")
    if row["unsigned_transaction_size"] != len(envelope):
        raise ValueError("wrong unsigned transaction size")
    expected_call = {
        "from": row["transaction"]["from"],
        "to": row["transaction"]["to"],
        "data": row["transaction"]["data"],
        "gas": hex(limit),
        "maxFeePerGas": hex(base + tip),
        "maxPriorityFeePerGas": hex(tip),
    }
    if row.get("budget_call") != {"transaction": expected_call, "response": row["response"]}:
        raise ValueError("execution with the budgeted gas limit and fee caps must match")
    fees = row["fee_reads"]
    total = limit * (base + tip)
    expected = [
        ("getL1FeeUpperBound(uint256)", "uint256", len(envelope)),
        ("getOperatorFee(uint256)", "uint256", limit),
    ]
    for signature, typ, arg in expected:
        record = fees[signature]
        if "response" not in record:
            return {"status": "fee_unavailable", "method": signature}
        if record["data"] != "0x" + encode_call(signature, [typ], [arg]).hex():
            raise ValueError("fee read is not bound to this transaction")
        raw = hex_bytes(record["response"])
        if len(raw) != 32:
            raise ValueError("malformed fee response")
        total += decode(["uint256"], raw)[0]
    net = int(result["profit_raw"]) - total
    return {
        "status": "historical_estimated_positive" if net > 0 else "not_positive_after_budget",
        "profit_weth_wei": result["profit_raw"],
        "total_fee_budget_wei": str(total),
        "net_eth_wei": str(net),
        "warning": "Historical eth_call and fee estimates; no inclusion, deployment or period P/L proven. WETH valued 1:1 in ETH; payout is WETH. Fee cap equals pinned base plus tip.",
    }


async def attach_fees(rpc, row, header, overrides):
    base, tip = int(header["baseFeePerGas"], 16), int(row["priority_fee_wei"])
    limit = (row["gas_estimate"] * 120 + 99) // 100
    envelope = transaction_envelope(row["transaction"], limit, base, tip)
    row.update(gas_limit=limit, unsigned_transaction="0x" + envelope.hex(), unsigned_transaction_size=len(envelope))
    row["fee_reads"] = {}
    for signature, typ, value in [
        ("getL1FeeUpperBound(uint256)", "uint256", len(envelope)),
        ("getL1Fee(bytes)", "bytes", envelope),
        ("getOperatorFee(uint256)", "uint256", limit),
    ]:
        data = encode_call(signature, [typ], [value])
        record = {"data": "0x" + data.hex()}
        try:
            record["response"] = await rpc.contract_call(ORACLE, data, row["block"])
        except RpcError as error:
            record.update(error=str(error), rpc_code=error.code, revert_data=error.data)
        row["fee_reads"][signature] = record
    bounded = {
        "from": row["transaction"]["from"],
        "to": row["transaction"]["to"],
        "data": row["transaction"]["data"],
        "gas": hex(limit),
        "maxFeePerGas": hex(base + tip),
        "maxPriorityFeePerGas": hex(tip),
    }
    raw = await rpc.simulation_call(bounded, row["block"], overrides)
    if raw != row["response"]:
        raise ValueError("budgeted gas limit changes the financed result")
    row["budget_call"] = {"transaction": bounded, "response": raw}
    row["assessment"] = net_weth(row, header)


def compile_probe(solc):
    with tempfile.TemporaryDirectory() as directory:
        command = [
            str(Path(solc).resolve()),
            "--optimize",
            "--optimize-runs",
            "200",
            "--evm-version",
            "cancun",
            "--bin-runtime",
            "--overwrite",
            "-o",
            directory,
            str(SOURCE),
        ]
        subprocess.run(command, cwd=REPO, check=True, capture_output=True)
        runtime = Path(directory, "FlashPairProbe.bin-runtime").read_text().strip()
    return runtime


async def probe(args, runtime):
    if args.start_block < 0 or not 1 <= args.end_block - args.start_block + 1 <= 300:
        raise ValueError("ordered range of at most 300 blocks required")
    if not 0 < args.amount_weth_raw < 2**127 or not 0 <= args.tip_wei or not 1 <= args.rounds <= 8:
        raise ValueError("invalid loan, tip or repetition count")
    overrides = {EXECUTOR: {"code": "0x" + runtime}, SENDER: {"balance": hex(10**18)}}
    out = {
        "chain_id": 8453,
        "rpc": args.rpc_url,
        "overrides": overrides,
        "source_sha256": hashlib.sha256((REPO / SOURCE).read_bytes()).hexdigest(),
        "runtime_keccak": "0x" + keccak(hex_bytes("0x" + runtime)).hex(),
        "compiler_flags": "solc 0.8.28 --optimize --optimize-runs 200 --evm-version cancun --bin-runtime",
        "rows": [],
        "canonical_rechecks": [],
        "scope": "Independent historical states, no own fills persisted between blocks; no live transactions.",
    }
    async with ReadOnlyRpc(args.rpc_url, timeout_seconds=35, requests_per_second=2) as rpc:
        if await rpc.chain_id() != 8453:
            raise ValueError("Base chain ID 8453 required")
        out["original_code"] = {}
        for address in (EXECUTOR, SENDER):
            code = await rpc.code_at(address, args.start_block)
            if code != "0x":
                raise ValueError("synthetic executor and EOA must have no original code")
            out["original_code"][address] = code
        out["initial_token_balances"] = {}
        for token in (USDC, WETH):
            raw = await rpc.contract_call(
                token, encode_call("balanceOf(address)", ["address"], [EXECUTOR]), args.start_block
            )
            if int(raw, 16) != 0:
                raise ValueError("synthetic executor must start without trading capital")
            out["initial_token_balances"][token] = raw
        previous = None
        for block in range(args.start_block, args.end_block + 1):
            header = await rpc.sealed_block(block)
            if not header or not header.get("hash") or int(header["number"], 16) != block:
                raise ValueError("missing canonical block")
            if previous is not None and header["parentHash"] != previous["hash"]:
                raise ValueError("block ancestry changed")
            previous = header
            data = encode_call(
                SIGNATURE,
                ["(uint256,uint256,bool,address)", "uint256"],
                [(args.amount_weth_raw, args.rounds, True, MAKER), 0],
            )
            tx = {
                "from": SENDER,
                "to": EXECUTOR,
                "data": "0x" + data.hex(),
                "gas": hex(4_000_000),
                "gasPrice": hex(int(header["baseFeePerGas"], 16) + args.tip_wei),
            }
            row = {
                "block": block,
                "block_hash": header["hash"],
                "header": header,
                "transaction": tx,
                "debt_asset": WETH,
                "amount_raw": str(args.amount_weth_raw),
                "rounds": args.rounds,
                "priority_fee_wei": str(args.tip_wei),
            }
            try:
                raw = await rpc.simulation_call(tx, block, overrides)
                row.update(financed_result(raw, args.amount_weth_raw, args.rounds))
                row["gas_estimate"] = await rpc.simulation_gas(tx, block, overrides)
                await attach_fees(rpc, row, header, overrides)
            except RpcError as error:
                row.update(error=str(error), rpc_code=error.code, revert_data=error.data)
            out["rows"].append(row)
        for row in out["rows"]:
            header = await rpc.sealed_block(row["block"])
            if not header or header["hash"] != row["block_hash"]:
                raise ValueError("canonical block replaced; discard results")
            out["canonical_rechecks"].append({"block": row["block"], "hash": header["hash"]})
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solc", required=True)
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--end-block", type=int, required=True)
    parser.add_argument("--amount-weth-raw", type=int, default=63_000_000_000_000_000)
    parser.add_argument("--tip-wei", type=int, default=1_000_000)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--rpc-url", default="https://mainnet.base.org")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(probe(args, compile_probe(args.solc)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(result, indent=2) + "\n").encode()
    args.output.write_bytes(gzip.compress(data, mtime=0) if str(args.output).endswith(".gz") else data)
    print(json.dumps([r.get("assessment", {"status": "execution_not_confirmed"}) for r in result["rows"]]))
