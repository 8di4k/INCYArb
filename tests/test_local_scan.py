import asyncio
import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest
from eth_abi import encode
from eth_utils import keccak

from incy_arb import local_scan
from incy_arb.cl_snapshot import BitmapTickMap, CLSnapshot
from incy_arb.flash_credit import NATIVE_CURRENCY as NATIVE, POOL_MANAGER
from incy_arb.local_scan import LocalV4Config, _LegCache, scan_local_v4, size_local_paths
from incy_arb.research import cycle_paths
from incy_arb.v4_state import V4_STATE_VIEW

A = "0x" + "11" * 20


class Model:
    block_hash = "0xaa"

    def __init__(self, pool, block):
        self.pool = pool
        self.block = block

    def quote(self, zero_for_one, amount):
        numerator = {3000: 4, 500: 2, 100: 1}[self.pool["fee_pips"]]
        return amount * numerator // 2 if zero_for_one else amount * 2 // numerator

    def record(self):
        return {"pool": self.pool, "block_number": self.block, "block_hash": self.block_hash}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    pools = {}
    for fee in (3000, 500, 100):
        key = (NATIVE, A, fee, 60, NATIVE)
        identity = "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [key])).hex()
        pools[identity] = {"currency0": NATIVE, "currency1": A, "fee_pips": fee, "tick_spacing": 60, "hooks": NATIVE}
    discovery = tmp_path / "discovery.json"
    state = tmp_path / "state.json"
    discovery.write_text(
        json.dumps({"chain_id": 57073, "pool_manager": POOL_MANAGER, "complete": True, "pools": pools})
    )
    state.write_text(
        json.dumps(
            {
                "chain_id": 57073,
                "state_view": V4_STATE_VIEW,
                "block_number": 100,
                "states": {key: {"readable": True, "liquidity": "1000"} for key in pools},
            }
        )
    )

    class Rpc:
        reorg = False

        def __init__(self, url):
            self.reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return 57073

        async def block_number(self):
            return 123

        async def sealed_block(self, block):
            assert block == 123
            self.reads += 1
            return {"hash": "0xbb" if self.reorg and self.reads > 1 else "0xaa"}

    async def load(rpc, venue, pool, block, words):
        assert block == 123
        return Model(pool, block)

    monkeypatch.setattr(local_scan, "ReadOnlyRpc", Rpc)
    monkeypatch.setattr(local_scan, "load_cl_snapshot", load)
    return LocalV4Config(discovery, state, 10, 1000, grid_points=7, refinement_iterations=0, validate_top=0)


def test_local_search_sizes_candidates_but_does_not_claim_unvalidated_net_profit(setup):
    result = asyncio.run(scan_local_v4(setup))
    assert result["structural_cycles"] == 6 and len(result["findings"]) == 3
    assert result["findings"][0]["amount_in"] == "1000" and result["findings"][0]["gross_pnl"] == "3000"
    assert result["execution_validation"] == [] and result["summary"]["simulated_positive_after_budget"] == 0
    assert result["quote_block_hash"] == "0xaa"


@pytest.mark.parametrize(
    "net_status,difference,expected",
    [("estimated_positive", "0", 1), ("not_positive_after_fee_budget", "0", 0), ("estimated_positive", "1", 0)],
)
def test_only_independent_matching_execution_and_fee_budget_promote_candidate(
    setup, monkeypatch, net_status, difference, expected
):
    called = []

    async def simulate(rpc, scan, index):
        called.append(index)
        return {"status": "success", "quote_difference": difference, "net_profit_assessment": {"status": net_status}}

    monkeypatch.setattr(local_scan, "simulate_finding", simulate)
    result = asyncio.run(scan_local_v4(replace(setup, validate_top=1)))
    assert called == [0] and result["summary"]["positive_local_gross"] == 3
    assert result["summary"]["simulated_positive_after_budget"] == expected


def test_fee_cap_filters_graph_before_loading_unused_pools(setup):
    result = asyncio.run(scan_local_v4(replace(setup, max_fee_pips=1000)))
    assert result["selected_pools"] == 2 and result["structural_cycles"] == 2
    assert len(result["findings"]) == 1


def test_mixed_block_snapshots_fail_before_search(setup, monkeypatch):
    parent = local_scan.ReadOnlyRpc

    class Reorg(parent):
        reorg = True

    monkeypatch.setattr(local_scan, "ReadOnlyRpc", Reorg)
    with pytest.raises(RuntimeError, match="block replaced"):
        asyncio.run(scan_local_v4(setup))


@pytest.mark.parametrize(
    "values",
    [
        {"minimum": 0},
        {"maximum": 1},
        {"max_steps": 5},
        {"max_fee_pips": 1_000_000},
        {"currencies": (A,)},
        {"validate_top": -1},
    ],
)
def test_invalid_local_search_configuration_is_rejected(setup, values):
    with pytest.raises(ValueError):
        asyncio.run(scan_local_v4(replace(setup, **values)))


def test_local_cache_is_bounded_without_changing_result():
    first = {"address": "p1", "token0": NATIVE, "fee_pips": 3000}
    second = {"address": "p2", "token0": NATIVE, "fee_pips": 500}
    models = {"p1": Model(first, 123), "p2": Model(second, 123)}
    cache = _LegCache(models, maximum=1)
    path = [("uniswap_v4", first, NATIVE, A), ("uniswap_v4", second, A, NATIVE)]
    assert cache.quote(path, 100) == 200 and cache.quote(path, 100) == 200
    assert len(cache.values) == 1 and cache.computed == 3 and cache.hits == 1


def test_captured_native_optimum_matches_independent_successful_evm_profit():
    root = Path(__file__).resolve().parents[1]
    report = json.loads(gzip.decompress((root / "docs/validation/native-local-core-59200457.json.gz").read_bytes()))
    models = {}
    pools = []
    for key, record in report["snapshots"].items():
        window = record["tick_window"]
        ticks = BitmapTickMap(
            {int(t): int(n) for t, n in window["liquidity_net"].items()},
            window["lo"],
            window["hi"],
            record["tick_spacing"],
        )
        models[key] = CLSnapshot(
            record["venue"],
            record["pool"],
            record["block_number"],
            record["block_hash"],
            int(record["sqrt_price_x96"]),
            record["tick"],
            int(record["liquidity"]),
            record["tick_spacing"],
            record["lp_fee"],
            record["protocol_fee"],
            ticks,
        )
        pools.append((record["venue"], record["pool"]))
    paths = cycle_paths(pools, (NATIVE,), 3)
    config = LocalV4Config(Path("unused"), Path("unused"), 10**9, 10**17)
    result = size_local_paths(paths, models, config)
    assert len(result["findings"]) == 1 and int(result["findings"][0]["gross_pnl"]) >= 2508382745
    execution = json.loads((root / "docs/validation/native-positive-before-fees-59200457.json").read_text())
    candidate = report["findings"][0]
    path = next(p for p in paths if [edge[1]["address"] for edge in p] == candidate["pools"])
    output = _LegCache(models).quote(path, int(candidate["amount_in"]))
    assert output - int(candidate["amount_in"]) == int(execution["simulated_gross_pnl"]) == 2508382745
    assert execution["net_profit_assessment"]["status"] == "not_positive_after_fee_budget"
