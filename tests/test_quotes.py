import json

import pytest

from eth_abi import encode

from incy_arb.quotes import V2QuoteRequest, constant_product_out, decode_slipstream_quote, quote_v2_snapshot


TOKEN0 = "0x0000000000000000000000000000000000000001"
TOKEN1 = "0x0000000000000000000000000000000000000002"
POOL = "0x00000000000000000000000000000000000000aa"


def test_constant_product_equation_matches_xarb2_volatile_path() -> None:
    # (1000 * 9970 * 20000) // (10000 * 10000 + 1000 * 9970)
    assert constant_product_out(1_000, 10_000, 20_000, 30) == 1_813
    assert constant_product_out(0, 10_000, 20_000, 30) is None
    assert constant_product_out(1, 1, 1, 10_000) is None


def test_quote_rejects_non_admitted_and_stable_pools(tmp_path) -> None:
    universe = {
        "chain_id": 57073,
        "inventory_block_number": 1,
        "inventory_block_timestamp": 2,
        "candidates": [{
            "venue": "velodrome_v2", "address": POOL, "token0": TOKEN0, "token1": TOKEN1,
            "reserve0": "10000", "reserve1": "20000", "fee_bps": 30,
            "stable": False,
        }],
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(universe), encoding="utf-8")
    result = quote_v2_snapshot(V2QuoteRequest(path, POOL, TOKEN0, 1_000))
    assert result["amount_out"] == "1813"
    with pytest.raises(ValueError, match="not an admitted"):
        quote_v2_snapshot(V2QuoteRequest(path, "0x00000000000000000000000000000000000000bb", TOKEN0, 1))
    universe["candidates"][0]["stable"] = True
    path.write_text(json.dumps(universe), encoding="utf-8")
    with pytest.raises(ValueError, match="stable V2"):
        quote_v2_snapshot(V2QuoteRequest(path, POOL, TOKEN0, 1))


def test_decode_slipstream_quoter_layout() -> None:
    data = encode(["uint256", "uint160", "uint32", "uint256"], [100, 200, 3, 400])
    assert decode_slipstream_quote(data) == {
        "amount_out": 100,
        "sqrt_price_x96_after": 200,
        "initialized_ticks_crossed": 3,
        "gas_estimate": 400,
    }
