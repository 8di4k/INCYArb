from eth_abi import encode

from incy_arb.v4_state import decode_slot0


def test_decode_slot0_uses_stateview_return_layout() -> None:
    encoded = encode(["uint160", "int24", "uint24", "uint24"], [123, -17, 45, 3000])
    assert decode_slot0(encoded) == {"sqrt_price_x96": 123, "tick": -17, "protocol_fee": 45, "lp_fee": 3000}
