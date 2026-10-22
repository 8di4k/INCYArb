from __future__ import annotations

from eth_abi import encode

from incy_arb.abi import ReadCall, decode_aggregate3, encode_aggregate3, encode_call, function_selector


def test_function_encoding_and_multicall_round_trip():
    assert encode_call("allPoolsLength()") == function_selector("allPoolsLength()")
    calls = [ReadCall("0x0000000000000000000000000000000000000001", b"\x12\x34", "one")]
    encoded = encode_aggregate3(calls)
    assert encoded[:4] == function_selector("aggregate3((address,bool,bytes)[])")

    response = "0x" + encode(["(bool,bytes)[]"], [[(True, b"\x00" * 31 + b"\x07")]]).hex()
    assert decode_aggregate3(response) == [(True, b"\x00" * 31 + b"\x07")]
