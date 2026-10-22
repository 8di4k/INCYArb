"""Offline cashflow accounting over an explicitly scoped Ink evidence archive."""

import argparse
import gzip
import json
from pathlib import Path

from incy_arb.competitor_cashflows import analyze_day


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = json.loads(gzip.decompress(raw) if args.input.suffix == ".gz" else raw)
    result = analyze_day(data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(result, indent=2) + "\n").encode()
    args.output.write_bytes(gzip.compress(raw, mtime=0) if args.output.suffix == ".gz" else raw)
    print(
        json.dumps(
            {
                "transactions": len(result["transactions"]),
                "executors": len(result["by_executor"]),
                "senders": len(result["by_sender"]),
                "ownership_verified": False,
            }
        )
    )
