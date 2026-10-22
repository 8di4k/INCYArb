"""Read-only, bounded Base quote screen for Elfomo/Biconomy; never submits trades.

Independent end-of-block quotes are candidate screens, not atomic execution or
continuous opportunity durations. A positive row excludes gas and flash costs.
"""

import argparse
import asyncio
import gzip
import json
from pathlib import Path

from eth_abi.exceptions import DecodingError
from eth_utils import is_address, keccak

from incy_arb.abi import decode_result, encode_call, hex_bytes
from incy_arb.rpc import ReadOnlyRpc, RpcError

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
VENUES = {
    "elfomo": ("0xf0f0f0f0fb0d738452efd03a28e8be14c76d5f73", "getAmountOut(address,address,uint256)"),
    "biconomy": ("0x0000009e5f98cdb4dd1e1d2e15b220217c88b3b8", "quote(address,address,uint256)"),
}


def positive_runs(rows):
    """Consecutive positive block snapshots, split by gaps and failed quotes.

    The timestamp span is descriptive; snapshots do not certify intervening
    intra-block states, executable duration, or separate opportunities.
    """
    runs, active = [], None
    for row in sorted(rows, key=lambda r: r["block"]):
        positive = row.get("difference_raw") is not None and int(row["difference_raw"]) > 0
        if not positive:
            active = None
            continue
        if active is None or row["block"] != active["last_block"] + 1:
            active = {
                "first_block": row["block"],
                "first_timestamp": row["timestamp"],
                "samples": 0,
            }
            runs.append(active)
        active.update(last_block=row["block"], last_timestamp=row["timestamp"], samples=active["samples"] + 1)
        active["observed_span_seconds"] = active["last_timestamp"] - active["first_timestamp"]
    return runs


async def screen(start, end, step, amount, caller, rpc_url):
    if start < 0 or end < start or step < 1 or (end - start) // step + 1 > 300:
        raise ValueError("ordered nonnegative range of at most 300 sampled blocks required")
    if not 0 < amount < 2**256 or not is_address(caller) or not int(caller, 16):
        raise ValueError("positive raw USDC amount and explicit nonzero caller required")
    reads, rows, headers, runtimes = [], [], [], {}
    async with ReadOnlyRpc(rpc_url, timeout_seconds=35, requests_per_second=1.5) as rpc:
        if await rpc.chain_id() != 8453:
            raise ValueError("this deployment screen requires Base chain 8453")
        for name, (address, _) in VENUES.items():
            code = await rpc.code_at(address, start)
            if code == "0x":
                raise ValueError(f"{name} has no runtime at the first sampled block")
            runtimes[name] = "0x" + keccak(hex_bytes(code)).hex()

        async def quote(name, token_in, token_out, value, block):
            address, signature = VENUES[name]
            transaction = {
                "from": caller,
                "to": address,
                "data": "0x"
                + encode_call(signature, ["address", "address", "uint256"], [token_in, token_out, value]).hex(),
            }
            record = {"venue": name, "block": block, "transaction": transaction}
            reads.append(record)
            try:
                raw = await rpc.simulation_call(transaction, block, {})
                record["response"] = raw
                value = decode_result(["uint256"], hex_bytes(raw))[0]
                if len(hex_bytes(raw)) != 32:
                    raise ValueError("noncanonical quote response")
                record["status"] = "positive_quote" if value > 0 else "zero_quote"
                return value if value > 0 else None
            except RpcError as error:
                record.update(
                    status="quote_reverted" if error.code == 3 else "rpc_error",
                    code=error.code,
                    error=str(error),
                    data=error.data,
                )
            except (ValueError, OverflowError, DecodingError) as error:
                record.update(status="malformed_response", error=str(error))
            return None

        previous = None
        for block in range(start, end + 1, step):
            header = await rpc.sealed_block(block)
            if not header or int(header["number"], 16) != block or not header.get("hash"):
                raise RuntimeError("cannot pin sampled block")
            if previous is not None and step == 1 and header["parentHash"] != previous["hash"]:
                raise RuntimeError("sampled block ancestry changed")
            headers.append(header)
            for buy, sell in (("elfomo", "biconomy"), ("biconomy", "elfomo")):
                middle = await quote(buy, USDC, WETH, amount, block)
                output = await quote(sell, WETH, USDC, middle, block) if middle is not None else None
                rows.append(
                    {
                        "block": block,
                        "timestamp": int(header["timestamp"], 16),
                        "buy": buy,
                        "sell": sell,
                        "middle_raw": str(middle) if middle is not None else None,
                        "output_raw": str(output) if output is not None else None,
                        "difference_raw": str(output - amount) if output is not None else None,
                    }
                )
            previous = header
        # Every sampled block is rechecked after the complete range, not just its endpoints.
        for header in headers:
            after = await rpc.sealed_block(int(header["number"], 16))
            if not after or after.get("hash") != header["hash"]:
                raise RuntimeError("sampled block replaced during reads")
    return {
        "chain_id": 8453,
        "rpc": rpc_url,
        "caller": caller,
        "amount_usdc_raw": str(amount),
        "step": step,
        "venues": VENUES,
        "runtime_hashes_at_start": runtimes,
        "blocks": headers,
        "reads": reads,
        "cycles": rows,
        "positive_snapshot_runs": {buy: positive_runs([r for r in rows if r["buy"] == buy]) for buy in VENUES},
        "execution_verified": False,
        "oracle_publish_lag_verified": False,
        "warning": "Independent quote screen only; no swaps, financing, gas, delivery or continuous duration verified. Quoter caller, tx.origin and tip can differ from execution. Reverts and RPC failures are not negative-price observations.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--end-block", type=int, required=True)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--amount-usdc-raw", type=int, default=100_000000)
    parser.add_argument("--caller", required=True, help="Explicit diagnostic quote caller, not proof of swap access")
    parser.add_argument("--rpc-url", default="https://mainnet.base.org")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(
        screen(args.start_block, args.end_block, args.step, args.amount_usdc_raw, args.caller, args.rpc_url)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(result, indent=2) + "\n").encode()
    args.output.write_bytes(gzip.compress(raw, mtime=0) if str(args.output).endswith(".gz") else raw)
    print(json.dumps(result["positive_snapshot_runs"]))
