from incy_arb.v4_inventory import parse_initialize


def test_parse_initialize_retains_canonical_source_location() -> None:
    event = {
        "block_number": 42,
        "block_hash": "0xblock",
        "index": 7,
        "transaction_hash": "0xtx",
        "block_timestamp": "2026-10-22T16:20:44.000000Z",
        "decoded": {
            "abi": {"name": "Initialize"},
            "parameters": [
                {"name": "id", "value": "0xABCD"},
                {"name": "currency0", "value": "0x0000000000000000000000000000000000000001"},
                {"name": "currency1", "value": "0x0000000000000000000000000000000000000002"},
                {"name": "fee", "value": "3000"},
                {"name": "tickSpacing", "value": "-60"},
                {"name": "hooks", "value": "0x0000000000000000000000000000000000000000"},
                {"name": "sqrtPriceX96", "value": "123"},
                {"name": "tick", "value": "-12"},
            ],
        },
    }
    result = parse_initialize(event)
    assert result is not None
    assert result["pool_id"] == "0xabcd"
    assert result["tick_spacing"] == -60
    assert result["source"] == {
        "block_number": 42,
        "block_hash": "0xblock",
        "log_index": 7,
        "transaction_hash": "0xtx",
        "block_timestamp": "2026-10-22T16:20:44.000000Z",
    }


def test_parse_initialize_rejects_incomplete_logs() -> None:
    assert parse_initialize({"decoded": {"abi": {"name": "Initialize"}, "parameters": []}}) is None
