import asyncio
import copy
import io
import json

import pytest
from eth_utils import keccak

from incy_arb.observer import _verify_and_record
from incy_arb.rpc import RpcError
from incy_arb.subblocks import PayloadCollector, verify_receipts

H = "0x" + "aa" * 32
A = "0x" + "11" * 20


def fixture():
    raws = ["0x02beef", "0x04cafe"]
    receipts = {}
    for i, raw in enumerate(raws):
        tx = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        receipts[tx] = {
            "status": "0x1",
            "type": hex(bytes.fromhex(raw[2:])[0]),
            "cumulativeGasUsed": hex((i + 1) * 21000),
            "logs": [{"address": A, "topics": ["0x" + "33" * 32], "data": data} for data in ("0x1234", "0x5678")]
            if i == 0
            else [],
        }
    source = {
        "payload_id": "a",
        "index": 0,
        "base": {"block_number": "0xa", "parent_hash": "0xparent", "timestamp": "0x10", "base_fee_per_gas": "0x20"},
        "metadata": {"block_number": 10, "receipts": receipts},
        "diff": {"transactions": raws},
    }
    collector = PayloadCollector()
    collector.add(source)
    next_payload = {
        "payload_id": "b",
        "index": 0,
        "base": {"block_number": "0xb"},
        "metadata": {"block_number": 11, "receipts": {}},
        "diff": {"transactions": []},
    }
    payload = collector.add(next_payload)[0]
    canonical = []
    for i, tx in enumerate(payload.transactions):
        canonical.append(
            {
                **copy.deepcopy(tx.receipt),
                "transactionHash": tx.hash,
                "blockHash": H,
                "blockNumber": "0xa",
                "transactionIndex": hex(i),
            }
        )
    return payload, canonical


def block(payload):
    return {
        "number": "0xa",
        "parentHash": "0xparent",
        "timestamp": "0x10",
        "baseFeePerGas": "0x20",
        "hash": H,
        "transactions": [tx.hash for tx in payload.transactions],
    }


def test_ordered_receipt_logs_and_numeric_fields_match_sealed_receipts():
    payload, canonical = fixture()
    canonical.reverse()  # Receipt array order is not trusted; transaction indices are checked.
    result = verify_receipts(payload, canonical, H)
    assert result["verified"] and result["checked_transactions"] == 2 and result["checked_logs"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "0x0"),
        ("type", "0x1"),
        ("cumulativeGasUsed", "0xdead"),
        ("blockHash", "0x" + "bb" * 32),
        ("blockNumber", "0xb"),
        ("transactionIndex", "0x1"),
    ],
)
def test_receipt_metadata_mismatch_invalidates_logs(field, value):
    payload, canonical = fixture()
    canonical[0][field] = value
    assert not verify_receipts(payload, canonical, H)["verified"]


@pytest.mark.parametrize("mutation", ["address", "topic", "data", "order", "removed", "missing", "duplicate"])
def test_altered_omitted_or_reordered_receipt_evidence_is_rejected(mutation):
    payload, canonical = fixture()
    if mutation == "address":
        canonical[0]["logs"][0]["address"] = "0x" + "22" * 20
    if mutation == "topic":
        canonical[0]["logs"][0]["topics"] = ["0x" + "44" * 32]
    if mutation == "data":
        canonical[0]["logs"][0]["data"] = "0xabcd"
    if mutation == "order":
        canonical[0]["logs"].reverse()
    if mutation == "removed":
        canonical[0]["logs"][0]["removed"] = True
    if mutation == "missing":
        canonical.pop()
    if mutation == "duplicate":
        canonical.append(canonical[0])
    assert not verify_receipts(payload, canonical, H)["verified"]


def test_malformed_log_is_an_unverified_result_instead_of_a_crash():
    payload, canonical = fixture()
    canonical[0]["logs"][0]["data"] = "0xnothex"
    assert not verify_receipts(payload, canonical, H)["verified"]


def test_extra_receipts_cannot_inject_events_without_a_raw_transaction():
    payload, canonical = fixture()
    source = copy.deepcopy(payload.messages[0])
    source["metadata"]["receipts"]["0x" + "ff" * 32] = {"status": "0x1", "logs": []}
    collector = PayloadCollector()
    collector.add(source)
    payload = collector._active
    assert not payload.valid and any("extra receipts" in error for error in payload.errors)


class Rpc:
    def __init__(self, payload, receipts):
        self.payload = payload
        self.receipts = receipts
        self.reads = 0
        self.reorg = False

    async def sealed_block(self, number):
        self.reads += 1
        result = block(self.payload)
        if self.reorg and self.reads > 1:
            result["hash"] = "0x" + "bb" * 32
        return result

    async def block_receipts(self, number):
        if isinstance(self.receipts, Exception):
            raise self.receipts
        return self.receipts


def test_observer_only_marks_strong_verification_after_receipts_and_hash_check():
    payload, receipts = fixture()
    output = io.StringIO()
    result = asyncio.run(_verify_and_record(payload, Rpc(payload, receipts), output, asyncio.Semaphore(1)))
    assert result["sealed_verification"]["verified"] and result["receipt_verification"]["verified"]
    assert json.loads(output.getvalue())["receipt_verification"]["block_hash"] == H


@pytest.mark.parametrize("failure", ["rpc", "reorg"])
def test_receipt_failure_never_falls_back_to_hash_only_verified(failure):
    payload, receipts = fixture()
    rpc = Rpc(payload, receipts)
    if failure == "rpc":
        rpc.receipts = RpcError("unsupported method", code=-32601)
    else:
        rpc.reorg = True
    result = asyncio.run(_verify_and_record(payload, rpc, io.StringIO(), asyncio.Semaphore(1)))
    assert not result["sealed_verification"]["verified"] and not result["receipt_verification"]["verified"]


def test_explicit_hash_only_capture_does_not_claim_verified_receipts():
    payload, receipts = fixture()
    rpc = Rpc(payload, AssertionError("must not read receipts"))
    result = asyncio.run(_verify_and_record(payload, rpc, io.StringIO(), asyncio.Semaphore(1), False))
    assert result["sealed_verification"]["verified"]
    assert not result["receipt_verification"]["requested"] and not result["receipt_verification"]["verified"]


def test_failed_transaction_cannot_have_retained_logs():
    payload, canonical = fixture()
    payload.transactions[0].receipt["status"] = "0x0"
    canonical[0]["status"] = "0x0"
    assert not verify_receipts(payload, canonical, H)["verified"]
