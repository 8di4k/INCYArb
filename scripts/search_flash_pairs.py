"""Bounded Base flash-funded search across advertised Biconomy pairs and Elfomo.

Every candidate is one complete eth_call, including repayment, WETH conversion
and payout. Independent historical states do not prove future or periodic income.
"""

import argparse
import asyncio
import gzip
import hashlib
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

from eth_abi import decode
from eth_utils import keccak

from incy_arb.abi import encode_call, hex_bytes
from incy_arb.rpc import ReadOnlyRpc, RpcError

_spec = importlib.util.spec_from_file_location("base_flash_probe", Path(__file__).with_name("probe_flash_pair.py"))
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
REPO = Path(__file__).resolve().parents[1]
SOURCE = Path("docs/validation/fresh-oracle-search/FlashPairSearch.sol")
ELF = "0xf0f0f0f0fb0d738452efd03a28e8be14c76d5f73"
BICO = "0x0000009e5f98cdb4dd1e1d2e15b220217c88b3b8"
BTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
TOKENS = {base.WETH, base.USDC, BTC}
SIGNATURE = "probe((address,address,uint256,bool,address),uint256)"
SIZES = [1, 10, 25, 50, 100, 125, 150, 152, 200, 500, 1000]


def score(row, header):
    if "response" not in row:
        return {"status": "execution_not_confirmed"}
    if row["block"] != int(header["number"], 16) or row["block_hash"] != header["hash"]:
        raise ValueError("different canonical block")
    calldata = hex_bytes(row["transaction"]["data"])
    if calldata[:4] != keccak(text=SIGNATURE)[:4]:
        raise ValueError("wrong financed call selector")
    plan, minimum = decode(["(address,address,uint256,bool,address)", "uint256"], calldata[4:])
    if plan != (row["debt_asset"], row["middle_asset"], int(row["amount_raw"]), row["elf_first"], row["maker"]):
        raise ValueError("declared route differs from simulated calldata")
    profit_weth, profit_debt, before, after = decode(["uint256"] * 4, hex_bytes(row["response"]))
    if before != after or before < int(row["amount_raw"]):
        raise ValueError("loan principal not reconciled")
    if profit_weth < minimum:
        raise ValueError("profit floor not met")
    if (str(profit_weth), str(profit_debt), str(before), str(after)) != (
        row["profit_weth_raw"],
        row["profit_debt_raw"],
        row["manager_before"],
        row["manager_after"],
    ):
        raise ValueError("decoded result changed")
    if row["debt_asset"] == base.WETH and profit_weth != profit_debt:
        raise ValueError("WETH profit changed without conversion")
    limit = (row["gas_estimate"] * 120 + 99) // 100
    if row["gas_limit"] != limit:
        raise ValueError("20% gas reserve required")
    pinned_base, tip = int(header["baseFeePerGas"], 16), int(row["priority_fee_wei"])
    envelope = base.transaction_envelope(row["transaction"], limit, pinned_base, tip)
    if row["unsigned_transaction"] != "0x" + envelope.hex() or row["unsigned_transaction_size"] != len(envelope):
        raise ValueError("fee envelope changed")
    bounded = {key: row["transaction"][key] for key in ("from", "to", "data")}
    bounded.update(gas=hex(limit), maxFeePerGas=hex(pinned_base + tip), maxPriorityFeePerGas=hex(tip))
    if row.get("budget_call") != {"transaction": bounded, "response": row["response"]}:
        raise ValueError("budget-capped execution required")
    total = limit * (pinned_base + tip)
    for signature, argument in [("getL1FeeUpperBound(uint256)", len(envelope)), ("getOperatorFee(uint256)", limit)]:
        fee = row["fee_reads"][signature]
        if "response" not in fee:
            return {"status": "fee_unavailable"}
        if fee["data"] != "0x" + encode_call(signature, ["uint256"], [argument]).hex():
            raise ValueError("wrong fee query")
        raw = hex_bytes(fee["response"])
        if len(raw) != 32:
            raise ValueError("malformed fee")
        total += decode(["uint256"], raw)[0]
    net = profit_weth - total
    return {
        "status": "historical_estimated_positive" if net > 0 else "not_positive_after_budget",
        "fee_budget_wei": str(total),
        "net_eth_wei": str(net),
    }


async def enrich(rpc, row, header, overrides):
    gas = await rpc.simulation_gas(row["transaction"], row["block"], overrides)
    limit = (gas * 120 + 99) // 100
    pinned_base, tip = int(header["baseFeePerGas"], 16), int(row["priority_fee_wei"])
    envelope = base.transaction_envelope(row["transaction"], limit, pinned_base, tip)
    row.update(
        gas_estimate=gas,
        gas_limit=limit,
        unsigned_transaction="0x" + envelope.hex(),
        unsigned_transaction_size=len(envelope),
        fee_reads={},
    )
    for signature, typ, value in [
        ("getL1FeeUpperBound(uint256)", "uint256", len(envelope)),
        ("getL1Fee(bytes)", "bytes", envelope),
        ("getOperatorFee(uint256)", "uint256", limit),
    ]:
        data = encode_call(signature, [typ], [value])
        record = {"data": "0x" + data.hex()}
        try:
            record["response"] = await rpc.contract_call(base.ORACLE, data, row["block"])
        except RpcError as error:
            record.update(error=str(error), rpc_code=error.code, revert_data=error.data)
        row["fee_reads"][signature] = record
    tx = {key: row["transaction"][key] for key in ("from", "to", "data")}
    tx.update(gas=hex(limit), maxFeePerGas=hex(pinned_base + tip), maxPriorityFeePerGas=hex(tip))
    response = await rpc.simulation_call(tx, row["block"], overrides)
    if response != row["response"]:
        raise ValueError("budgeted execution changes output")
    row["budget_call"] = {"transaction": tx, "response": response}
    row["assessment"] = score(row, header)


def decode_execution_error(error, amount):
    result = {"error": str(error), "rpc_code": error.code, "revert_data": error.data}
    if error.code != 3 or not isinstance(error.data, str):
        return result
    raw = hex_bytes(error.data)
    if raw[:4] == keccak(text="Shortfall(uint256,uint256)")[:4]:
        balance, loan = decode(["uint256", "uint256"], raw[4:])
        if loan != amount or balance >= loan:
            raise ValueError("malformed shortfall")
        result["negative_gross_debt_raw"] = str(balance - loan)
    elif raw[:4] == keccak(text="LegFailed(uint256,bytes)")[:4]:
        stage, nested = decode(["uint256", "bytes"], raw[4:])
        result.update(failed_stage=stage, nested_revert="0x" + nested.hex())
    return result


def compile_search(solc):
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(
            [
                str(Path(solc).resolve()),
                "--optimize",
                "--evm-version",
                "cancun",
                "--bin-runtime",
                "--overwrite",
                "-o",
                directory,
                str(SOURCE),
            ],
            cwd=REPO,
            check=True,
            capture_output=True,
        )
        return Path(directory, "FlashPairSearch.bin-runtime").read_text().strip()


async def search(args, runtime):
    if args.tip_wei < 0:
        raise ValueError("nonnegative tip required")
    overrides = {base.EXECUTOR: {"code": "0x" + runtime}, base.SENDER: {"balance": hex(10**18)}}
    out = {
        "chain_id": 8453,
        "rpc": args.rpc_url,
        "overrides": overrides,
        "catalog_reads": [],
        "amount_reads": [],
        "rows": [],
        "canonical_rechecks": [],
        "source_sha256": hashlib.sha256((REPO / SOURCE).read_bytes()).hexdigest(),
        "runtime_keccak": "0x" + keccak(hex_bytes("0x" + runtime)).hex(),
        "scope": "Three allowlisted assets, two propAMMs; independent end-of-block states, WETH payout. No live or periodic-income proof.",
    }
    async with ReadOnlyRpc(args.rpc_url, timeout_seconds=35, requests_per_second=2) as rpc:
        if await rpc.chain_id() != 8453:
            raise ValueError("Base chain ID required")
        block = args.block_number if args.block_number is not None else (await rpc.block_number()) - 2
        header = await rpc.sealed_block(block)
        if not header or int(header["number"], 16) != block:
            raise ValueError("canonical block unavailable")
        out["header"] = header
        out["original_code"] = {}
        for address in (base.EXECUTOR, base.SENDER):
            code = await rpc.code_at(address, block)
            if code != "0x":
                raise ValueError("synthetic caller and sender must have no original code")
            out["original_code"][address] = code

        async def read(target, signature, types, arguments, returns):
            data = encode_call(signature, types, arguments)
            raw = await rpc.contract_call(target, data, block)
            out["catalog_reads"].append({"target": target, "data": "0x" + data.hex(), "response": raw})
            return decode(returns, hex_bytes(raw))

        pairs = (await read(BICO, "getPairs()", [], [], ["(address,address)[]"]))[0]
        makers = (await read(BICO, "makers()", [], [], ["address[]"]))[0]
        if not 1 <= len(makers) <= 16 or len(pairs) > 100:
            raise ValueError("catalog exceeds research bounds")
        pairs = [pair for pair in pairs if set(pair) <= TOKENS and pair[0] != pair[1]]
        out["selected_pairs"] = pairs
        boards = {}
        for pair in pairs:
            for token_in, token_out in (pair, pair[::-1]):
                boards[token_in, token_out] = []
                for maker in makers:
                    result = await read(
                        BICO,
                        "board(address,address,address)",
                        ["address"] * 3,
                        [maker, token_in, token_out],
                        ["uint256[]", "uint256[]", "uint256", "uint256", "uint256"],
                    )
                    if result[3] > 0:
                        boards[token_in, token_out].append((maker, result[3]))
        amounts = {}
        for token in TOKENS:
            amounts[token] = []
            for size in SIZES:
                if token == base.USDC:
                    amounts[token].append((str(size), size * 10**6))
                    continue
                data = encode_call(
                    "getAmountOut(address,address,uint256)",
                    ["address", "address", "uint256"],
                    [base.USDC, token, size * 10**6],
                )
                record = {"token": token, "nominal_usdc": size, "data": "0x" + data.hex()}
                try:
                    raw = await rpc.contract_call(ELF, data, block)
                    amount = decode(["uint256"], hex_bytes(raw))[0]
                    record.update(response=raw, amount_raw=str(amount))
                    if amount > 0:
                        amounts[token].append((str(size), amount))
                except RpcError as error:
                    record.update(error=str(error), rpc_code=error.code, revert_data=error.data)
                out["amount_reads"].append(record)
        plans = []
        for pair in pairs:
            for debt, middle in (pair, pair[::-1]):
                for elf_first in (False, True):
                    direction = (middle, debt) if elf_first else (debt, middle)
                    active = boards[direction]
                    candidates = [maker for maker, _ in active] or [makers[0]]
                    if len(active) > 1:
                        candidates.append("0x" + "00" * 20)  # Compare merged venue execution too.
                    for maker in candidates:
                        sizes = list(amounts[debt])
                        if not elf_first:
                            cap = next((remaining for address, remaining in active if address == maker), None)
                            if cap:
                                sizes.extend(
                                    [("cap_99pct", cap * 99 // 100), ("cap_exact", cap), ("cap_plus_one", cap + 1)]
                                )
                        for label, amount in sizes:
                            plans.append((debt, middle, amount, elf_first, maker, label))
        if len(plans) > 512:
            raise ValueError("more than 512 probes; narrow the research universe")
        for debt, middle, amount, elf_first, maker, label in plans:
            data = encode_call(
                SIGNATURE,
                ["(address,address,uint256,bool,address)", "uint256"],
                [(debt, middle, amount, elf_first, maker), 0],
            )
            tx = {
                "from": base.SENDER,
                "to": base.EXECUTOR,
                "data": "0x" + data.hex(),
                "gas": hex(5_000_000),
                "gasPrice": hex(int(header["baseFeePerGas"], 16) + args.tip_wei),
            }
            row = {
                "block": block,
                "block_hash": header["hash"],
                "debt_asset": debt,
                "middle_asset": middle,
                "amount_raw": str(amount),
                "nominal_usdc": label,
                "elf_first": elf_first,
                "maker": maker,
                "priority_fee_wei": str(args.tip_wei),
                "transaction": tx,
            }
            try:
                raw = await rpc.simulation_call(tx, block, overrides)
                values = decode(["uint256"] * 4, hex_bytes(raw))
                row.update(
                    response=raw,
                    profit_weth_raw=str(values[0]),
                    profit_debt_raw=str(values[1]),
                    manager_before=str(values[2]),
                    manager_after=str(values[3]),
                )
                await enrich(rpc, row, header, overrides)
            except RpcError as error:
                row.update(decode_execution_error(error, amount))
            out["rows"].append(row)
            if len(out["rows"]) % 12 == 0:
                print(f"Completed {len(out['rows'])}/{len(plans)}", flush=True)
        if (await rpc.sealed_block(block))["hash"] != header["hash"]:
            raise ValueError("canonical block replaced; discard scan")
        out["canonical_rechecks"].append({"block": block, "hash": header["hash"]})
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solc", required=True)
    parser.add_argument("--block-number", type=int)
    parser.add_argument("--tip-wei", type=int, default=1_000_000)
    parser.add_argument("--rpc-url", default="https://mainnet.base.org")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(search(args, compile_search(args.solc)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(result, indent=2) + "\n").encode()
    args.output.write_bytes(gzip.compress(payload, mtime=0) if str(args.output).endswith(".gz") else payload)
    ranked = sorted(
        (r for r in result["rows"] if "net_eth_wei" in r.get("assessment", {})),
        key=lambda r: int(r["assessment"]["net_eth_wei"]),
        reverse=True,
    )
    print(
        json.dumps(
            [
                {key: r[key] for key in ("debt_asset", "middle_asset", "amount_raw", "elf_first", "assessment")}
                for r in ranked[:5]
            ]
        )
    )
