import asyncio
import copy
import io
import json

import pytest

from incy_arb.observer import _verify_and_record
from incy_arb.subblocks import PayloadCollector, verify_sealed
from incy_arb.timing import payload_timing, read_timing_capture, summarize_timing


def payload():
    collector = PayloadCollector()
    for index in (0, 1):
        message = {
            "payload_id": "a",
            "index": index,
            "metadata": {"block_number": 10, "receipts": {}},
            "diff": {"transactions": []},
        }
        if not index:
            message["base"] = {
                "block_number": "0xa",
                "parent_hash": "0xparent",
                "timestamp": "0x10",
                "base_fee_per_gas": "0x20",
            }
        collector.add(message)
    p = collector.active_payload
    p.message_timings = [
        {"index": 0, "received_monotonic": 100.0, "received_at_unix": 17.0},
        {"index": 1, "received_monotonic": 100.2, "received_at_unix": 17.2},
    ]
    return p


def block():
    return {
        "number": "0xa",
        "hash": "0x" + "aa" * 32,
        "parentHash": "0xparent",
        "timestamp": "0x10",
        "baseFeePerGas": "0x20",
        "transactions": [],
    }


def test_receive_intervals_use_monotonic_clock_despite_utc_jump():
    p = payload()
    p.message_timings[1]["received_at_unix"] = -1000
    result = payload_timing(p, queued=100.3, started=100.8, finished=101.8)
    assert result["complete_arrivals"]
    assert result["inter_arrival_seconds"] == pytest.approx([0.2])
    assert result["payload_arrival_span_seconds"] == pytest.approx(0.2)
    assert result["verification_queue_wait_seconds"] == pytest.approx(0.5)
    assert result["verification_complete_after_last_arrival_seconds"] == pytest.approx(1.6)
    assert result["last_arrival_minus_header_seconds"] == -1016


@pytest.mark.parametrize("change", ["missing", "wrong_index", "backward_monotonic", "nonfinite", "missing_field"])
def test_missing_or_invalid_timing_does_not_create_interval_measurements_or_change_state_verification(change):
    p = payload()
    if change == "missing":
        p.message_timings = []
    elif change == "wrong_index":
        p.message_timings[-1]["index"] = 3
    elif change == "backward_monotonic":
        p.message_timings[-1]["received_monotonic"] = 99
    elif change == "nonfinite":
        p.message_timings[-1]["received_monotonic"] = float("nan")
    elif change == "missing_field":
        p.message_timings[-1].pop("received_at_unix")
    result = payload_timing(p, 100.3, 100.8, 101.8)
    assert result["complete_arrivals"] is False and "inter_arrival_seconds" not in result
    assert verify_sealed(p, block())["verified"]


def test_missing_header_timestamp_does_not_crash_observer_timing():
    p = payload()
    p.base.pop("timestamp")
    result = payload_timing(p, 100.3, 100.8, 101.8)
    assert result["complete_arrivals"] and "first_arrival_minus_header_seconds" not in result
    assert result["payload_arrival_span_seconds"] == pytest.approx(0.2)


def test_old_captures_have_no_invented_arrival_times():
    result = summarize_timing([{"captured_at_unix": 1000, "sealed_verification": {"verified": True}}])
    assert result["summary"]["complete_timing_records"] == 0
    assert result["seconds"]["inter_arrival_seconds"] == {"samples": 0}


def test_summary_keeps_record_verification_counts_separate_from_arrival_coverage(tmp_path):
    p = payload()
    timing = payload_timing(p, 100.3, 100.8, 101.8)
    records = [
        {
            "block_number": 10,
            "timing": timing,
            "receipt_verification": {"verified": True},
            "sealed_verification": {"verified": True},
        },
        {"block_number": 20},
    ]
    path = tmp_path / "capture.jsonl"
    # Timing summaries allow discontinuous windows; they do not claim replay continuity.
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = summarize_timing(read_timing_capture(path))
    assert result["summary"] == {
        "records": 2,
        "complete_timing_records": 1,
        "canonical_verified_records": 1,
        "receipt_verified_records": 1,
    }
    assert result["seconds"]["verification_work_seconds"]["median"] == pytest.approx(1.0)


def test_recorded_timing_never_mutates_raw_stream_messages():
    p = payload()
    original = copy.deepcopy(p.messages)

    class Rpc:
        async def sealed_block(self, number):
            return block()

        async def block_receipts(self, number):
            return []

    output = io.StringIO()
    result = asyncio.run(_verify_and_record(p, Rpc(), output, asyncio.Semaphore(1)))
    assert result["sealed_verification"]["verified"] and result["receipt_verification"]["verified"]
    assert result["message_timings"] == p.message_timings and result["messages"] == original == p.messages
    assert result["timing"]["complete_arrivals"]
    assert json.loads(output.getvalue())["timing"] == result["timing"]
