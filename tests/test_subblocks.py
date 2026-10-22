from __future__ import annotations

from eth_utils import keccak

from incy_arb.subblocks import PayloadCollector, verify_sealed


def message(payload_id: str, block: int, index: int, raw_transactions: list[str]) -> dict:
    receipts = {}
    for raw in raw_transactions:
        tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        receipts[tx_hash] = {"status": "0x1", "logs": []}
    result = {
        "payload_id": payload_id,
        "index": index,
        "metadata": {"block_number": block, "receipts": receipts},
        "diff": {"transactions": raw_transactions},
    }
    if index == 0:
        result["base"] = {
            "block_number": hex(block),
            "parent_hash": "0xparent",
            "timestamp": "0x10",
            "base_fee_per_gas": "0x20",
        }
    return result


def sealed(payload) -> dict:
    return {
        "number": hex(payload.block_number),
        "transactions": [tx.hash for tx in payload.transactions],
        "parentHash": "0xparent",
        "timestamp": "0x10",
        "baseFeePerGas": "0x20",
    }


def test_ignores_mid_payload_and_finalizes_only_at_next_base():
    collector = PayloadCollector()
    assert collector.add(message("old", 9, 3, ["0x02dead"])) == []
    assert collector.ignored_partial_messages == 1

    assert collector.add(message("a", 10, 0, ["0x02beef"])) == []
    assert collector.add(message("a", 10, 1, ["0x04cafe"])) == []
    finished = collector.add(message("b", 11, 0, ["0x7e00"]))

    assert len(finished) == 1
    payload = finished[0]
    assert payload.indices == [0, 1]
    assert [tx.kind for tx in payload.transactions] == ["eip1559", "eip7702"]
    assert payload.valid
    assert verify_sealed(payload, sealed(payload))["verified"]


def test_missing_receipt_and_gap_prevent_verification():
    collector = PayloadCollector()
    first = message("a", 10, 0, ["0x02beef"])
    first["metadata"]["receipts"] = {}
    collector.add(first)
    collector.add(message("a", 10, 2, ["0x02cafe"]))
    payload = collector.add(message("b", 11, 0, []))[0]

    assert not payload.valid
    assert any("missing receipt" in error for error in payload.errors)
    assert not verify_sealed(payload, sealed(payload))["verified"]


def test_sealed_mismatch_is_rejected():
    collector = PayloadCollector()
    collector.add(message("a", 10, 0, ["0x02beef"]))
    payload = collector.add(message("b", 11, 0, []))[0]
    block = sealed(payload)
    block["transactions"] = ["0x" + "00" * 32]

    verdict = verify_sealed(payload, block)
    assert not verdict["verified"]
    assert "sealed transaction order or set differs" in verdict["differences"]
