import asyncio
import copy
import gzip
import json
from pathlib import Path

import pytest
from eth_abi import encode

from incy_arb.dyor import DYOR_30BPS_RUNTIME_HASH, DYORSWAP_FACTORY, dyor_fee_metadata
from incy_arb.quotes import constant_product_out
from incy_arb.research import _current_v2, _research_pools
from incy_arb.simulation import encode_simulation_route, simulate_finding

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def capture():
    return json.loads(gzip.decompress((ROOT / "docs/validation/dyor-fee-parity-59202817.json.gz").read_bytes()))


def test_runtime_recognition_uses_code_not_untrusted_saved_fee(capture):
    key, code = next(iter(capture["dyor_runtime_codes"].items()))

    class Rpc:
        async def code_at(self, address, block):
            assert address == key and block == capture["quote_block_number"]
            return code

    metadata = asyncio.run(dyor_fee_metadata(Rpc(), key, capture["quote_block_number"]))
    assert metadata["fee_bps"] == 30 and metadata["runtime_code_hash"] == DYOR_30BPS_RUNTIME_HASH


@pytest.mark.parametrize("code", ["0x", "0x60006000fd", "0x00"])
def test_unknown_runtime_has_no_fee(code):
    class Rpc:
        async def code_at(self, address, block):
            return code

    assert asyncio.run(dyor_fee_metadata(Rpc(), "pair", 123))["fee_bps"] is None


def candidate(capture):
    pool = copy.deepcopy(capture["fee_parity_attempts"][0]["candidate"]["simulation_route"]["steps"][0]["pool"])
    pool.update(
        factory=DYORSWAP_FACTORY,
        runtime_code_hash=DYOR_30BPS_RUNTIME_HASH,
        fee_bps=30,
        has_nonzero_reserves=True,
        recently_updated=True,
    )
    return pool


@pytest.mark.parametrize(
    "field,value",
    [("runtime_code_hash", "0xdead"), ("fee_bps", 29), ("factory", "0x" + "ff" * 20), ("recently_updated", False)],
)
def test_research_requires_recognized_recent_factory_pool(capture, field, value):
    pool = candidate(capture)
    assert _research_pools({"dyorswap_v2": [pool]}) == [("dyorswap_v2", pool)]
    pool[field] = value
    assert _research_pools({"dyorswap_v2": [pool]}) == []


def test_scan_rechecks_code_at_quote_block_and_drops_changed_runtime(capture):
    pool = candidate(capture)

    class Rpc:
        async def multicall(self, calls, block):
            assert block == 123 and len(calls) == 1
            return [(True, encode(["uint112", "uint112", "uint32"], [10**18, 10**18, 1]))]

        async def code_at(self, address, block):
            assert address == pool["address"] and block == 123
            return "0x00"

    assert asyncio.run(_current_v2(Rpc(), 123, [("dyorswap_v2", pool)])) == {}


def test_simulation_refuses_saved_route_when_pinned_runtime_is_unknown(capture):
    route = copy.deepcopy(capture["fee_parity_attempts"][0]["candidate"])
    route["simulation_route"]["steps"][0]["venue"] = "dyorswap_v2"

    class Rpc:
        async def chain_id(self):
            return 57073

        async def sealed_block(self, block):
            return {"hash": capture["quote_block_hash"]}

        async def code_at(self, address, block):
            return "0x00"

    scan = dict(capture, findings=[route])
    with pytest.raises(ValueError, match="recognized pinned runtime"):
        asyncio.run(simulate_finding(Rpc(), scan, 0))


def test_dyor_uses_existing_v2_executor_encoding_without_new_contract_adapter(capture):
    route = copy.deepcopy(capture["fee_parity_attempts"][0]["candidate"]["simulation_route"])
    expected = encode_simulation_route(route, 123)
    route["steps"][0]["venue"] = "dyorswap_v2"
    assert encode_simulation_route(route, 123) == expected


def test_independent_evm_captures_confirm_math_and_understated_fee_is_rejected(capture):
    matches = controls = 0
    for attempt in capture["fee_parity_attempts"]:
        simulation = attempt["simulation"]
        if attempt.get("negative_control"):
            if simulation.get("execution_error", {}).get("arguments") == ["Dyor: K"]:
                controls += 1
            continue
        if simulation.get("quote_difference") != "0":
            assert simulation["execution_error"]["revert_data"].startswith("0x90bfb865")
            continue  # Capacity failure is not fee-model confirmation.
        route = attempt["candidate"]["simulation_route"]
        value, amount = int(route["amount_in"]), int(route["amount_in"])
        for step in route["steps"]:
            pool = step["pool"]
            state = capture["v2_snapshots"][pool["address"]]
            a, b = (
                (state["reserve0"], state["reserve1"])
                if step["token_in"] == pool["token0"]
                else (state["reserve1"], state["reserve0"])
            )
            value = constant_product_out(value, a, b, pool["fee_bps"])
        assert value - amount == int(simulation["simulated_gross_pnl"])
        matches += 1
    assert matches == 11 and controls == 3


@pytest.mark.parametrize("chain,reorg", [(1, False), (57073, True), (57073, False)])
def test_inventory_chain_and_hash_are_checked_before_saving(tmp_path, monkeypatch, chain, reorg):
    from incy_arb import inventory

    class Rpc:
        def __init__(self, url):
            self.reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return chain

        async def block_number(self):
            return 123

        async def sealed_block(self, number):
            self.reads += 1
            return {"hash": "0xbb" if reorg and self.reads > 1 else "0xaa", "timestamp": "0x1"}

    async def pools(*args):
        return []

    async def tokens(*args):
        return {}

    monkeypatch.setattr(inventory, "ReadOnlyRpc", Rpc)
    for name in ("_v2_pools", "_slipstream_pools", "_uniswap_style_v2_pools"):
        monkeypatch.setattr(inventory, name, pools)
    monkeypatch.setattr(inventory, "_token_metadata", tokens)
    output = tmp_path / "inventory.json"
    if chain != 57073 or reorg:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(inventory.build_inventory(inventory.InventoryConfig(output=output)))
        assert not output.exists()
    else:
        result = asyncio.run(inventory.build_inventory(inventory.InventoryConfig(output=output)))
        assert result["block_hash"] == "0xaa" and output.exists()
