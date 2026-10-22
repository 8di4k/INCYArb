"""Local receive/verification timing; never treated as canonical state evidence."""

from __future__ import annotations

import math
import gzip
import json
from pathlib import Path
import statistics
from typing import Any

from .subblocks import Payload, _hex_int


def payload_timing(payload: Payload, queued: float, started: float | None, finished: float) -> dict[str, Any]:
    samples = payload.message_timings
    complete = False
    try:
        complete = bool(samples) and [s["index"] for s in samples] == payload.indices
        mono = [s["received_monotonic"] for s in samples]
        utc = [s["received_at_unix"] for s in samples]
        complete = (
            complete
            and all(math.isfinite(value) for value in mono + utc)
            and all(a <= b for a, b in zip(mono, mono[1:]))
        )
    except (KeyError, TypeError, ValueError):
        complete = False
    result = {
        "complete_arrivals": complete,
        "arrival_count": len(samples),
        "verification_queue_wait_seconds": started - queued if started is not None else None,
        "verification_work_seconds": finished - started if started is not None else None,
        "warning": "Local observer receive and processing times. Header timestamp deltas include clock skew and timestamp scheduling; they are not one-way network latency or an inclusion guarantee.",
    }
    if complete:
        result.update(
            inter_arrival_seconds=[b - a for a, b in zip(mono, mono[1:])],
            payload_arrival_span_seconds=mono[-1] - mono[0],
            verification_complete_after_last_arrival_seconds=finished - mono[-1],
        )
        try:
            timestamp = _hex_int(payload.base["timestamp"])
            result.update(
                first_arrival_minus_header_seconds=utc[0] - timestamp,
                last_arrival_minus_header_seconds=utc[-1] - timestamp,
            )
        except (KeyError, TypeError, ValueError):
            pass
    return result


def read_timing_capture(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0}
    ordered = sorted(values)
    return {
        "samples": len(values),
        "minimum": ordered[0],
        "median": statistics.median(ordered),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "maximum": ordered[-1],
    }


def summarize_timing(records: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [r for r in records if r.get("timing", {}).get("complete_arrivals")]
    fields = (
        "payload_arrival_span_seconds",
        "first_arrival_minus_header_seconds",
        "last_arrival_minus_header_seconds",
        "verification_queue_wait_seconds",
        "verification_work_seconds",
        "verification_complete_after_last_arrival_seconds",
    )
    distributions = {
        field: _distribution([r["timing"][field] for r in complete if r["timing"].get(field) is not None])
        for field in fields
    }
    distributions["inter_arrival_seconds"] = _distribution(
        [gap for r in complete for gap in r["timing"]["inter_arrival_seconds"]]
    )
    return {
        "source": "local_observer_timing_summary",
        "summary": {
            "records": len(records),
            "complete_timing_records": len(complete),
            "canonical_verified_records": sum(bool(r.get("sealed_verification", {}).get("verified")) for r in records),
            "receipt_verified_records": sum(bool(r.get("receipt_verification", {}).get("verified")) for r in records),
        },
        "seconds": distributions,
        "warning": "Local receive/processing measurements only. No sequencer emission timestamp, clock-sync calibration, one-way network latency, trade execution, or profitability measurement.",
    }
