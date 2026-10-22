import json

from incy_arb.universe import UniverseConfig, build_trade_universe


ADDRESS_A = "0x0000000000000000000000000000000000000001"
ADDRESS_B = "0x0000000000000000000000000000000000000002"
POOL_A = "0x00000000000000000000000000000000000000aa"
POOL_B = "0x00000000000000000000000000000000000000bb"


def test_universe_requires_both_allowlists_and_live_liquidity(tmp_path) -> None:
    inventory = {
        "chain_id": 57073,
        "block_number": 100,
        "block_timestamp": 200,
        "velodrome_v2": [
            {"address": POOL_A, "token0": ADDRESS_A, "token1": ADDRESS_B, "has_nonzero_reserves": True, "recently_updated": True},
            {"address": POOL_B, "token0": ADDRESS_A, "token1": ADDRESS_B, "has_nonzero_reserves": True, "recently_updated": False},
        ],
        "slipstream": [],
    }
    policy = {
        "schema_version": 1,
        "tokens": [ADDRESS_A, ADDRESS_B],
        "velodrome_v2_pools": [POOL_A, POOL_B],
        "slipstream_pools": [],
    }
    inventory_path = tmp_path / "inventory.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "universe.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    result = build_trade_universe(UniverseConfig(inventory_path, policy_path, output_path))
    assert [pool["address"] for pool in result["candidates"]] == [POOL_A]
    assert result["rejected"] == [{"venue": "velodrome_v2", "address": POOL_B, "reason": "stale_reserves"}]
