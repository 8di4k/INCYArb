from incy_arb.__main__ import _start_amount
from incy_arb.research import cycle_paths, overlapping_slipstream_pairs, xarb2_compatibility


def test_overlapping_pairs_require_two_liquid_pools_for_same_tokens() -> None:
    a, b, c = "0x01", "0x02", "0x03"
    pools = {"slipstream": [
        {"address": "p1", "token0": a, "token1": b, "has_positive_liquidity": True},
        {"address": "p2", "token0": b, "token1": a, "has_positive_liquidity": True},
        {"address": "p3", "token0": a, "token1": c, "has_positive_liquidity": True},
        {"address": "p4", "token0": a, "token1": c, "has_positive_liquidity": False},
    ]}
    assert [(x["address"], y["address"]) for x, y in overlapping_slipstream_pairs(pools)] == [("p1", "p2")]


def test_cycle_paths_matches_arbitr_simple_cycle_rules() -> None:
    pools = [("v2", {"address": name, "token0": a, "token1": b}) for name, a, b in (
        ("p1", "a", "b"), ("p2", "b", "c"), ("p3", "c", "a"), ("p4", "c", "d"),
    )]
    paths = cycle_paths(pools, ("a",), 4)
    assert {tuple(edge[1]["address"] for edge in path) for path in paths} == {
        ("p1", "p2", "p3"), ("p3", "p2", "p1")
    }


def test_start_amount_keeps_token_specific_raw_units() -> None:
    assert _start_amount("0xAbC=5000000") == ("0xabc", 5_000_000)


def test_xarb2_compatibility_rejects_velodrome_stable_math() -> None:
    path = [("velodrome_v2", {"stable": True}, "a", "b")]
    assert xarb2_compatibility(path) == (False, ["velodrome_stable_requires_new_adapter"])
    assert xarb2_compatibility([("uniswap_v2", {"fee_bps": 30}, "a", "b")]) == (True, [])
