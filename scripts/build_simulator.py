"""Rebuild the checked-in simulator with solc 0.8.28 (no network required)."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
solc = sys.argv[1] if len(sys.argv) > 1 else "solc"
version = subprocess.check_output([solc, "--version"], text=True)
if "0.8.28+commit.7893614a" not in version:
    raise SystemExit("solc 0.8.28+commit.7893614a is required")
sources = {f"src/{path.name}": {"content": path.read_text()} for path in sorted((ROOT / "contracts/src").glob("*.sol"))}
settings = {"optimizer": {"enabled": True, "runs": 1000}, "evmVersion": "cancun",
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object", "storageLayout"]}}}
result = subprocess.run([solc, "--standard-json"], input=json.dumps({"language": "Solidity", "sources": sources, "settings": settings}),
                        capture_output=True, text=True, check=True)
compiled = json.loads(result.stdout)
for error in compiled.get("errors", []):
    if error["severity"] == "error":
        raise SystemExit(error["formattedMessage"])
contract = compiled["contracts"]["src/InkArb.sol"]["InkArb"]
storage = {item["label"]: {"slot": item["slot"], "offset": item["offset"]} for item in contract["storageLayout"]["storage"]}
expected = {"owner": {"slot": "0", "offset": 0}, "beneficiary": {"slot": "1", "offset": 0},
            "paused": {"slot": "1", "offset": 20}, "isExecutor": {"slot": "2", "offset": 0}}
if storage != expected:
    raise SystemExit(f"Unexpected simulator storage layout: {storage}")
artifact = {"compiler": "0.8.28+commit.7893614a", "evm_version": "cancun", "optimizer_runs": 1000,
            "source_sha256": {name: hashlib.sha256(source["content"].encode()).hexdigest() for name, source in sources.items()},
            "storage_layout": storage, "creation_code": "0x" + contract["evm"]["bytecode"]["object"]}
output = ROOT / "src/incy_arb/artifacts/InkArb.json"
output.write_text(json.dumps(artifact, indent=2) + "\n")
print(f"{output}: {len(artifact['creation_code'][2:]) // 2} bytes")
