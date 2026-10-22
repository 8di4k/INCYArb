import asyncio
import copy
from dataclasses import replace

import pytest
from eth_abi import encode
from eth_utils import keccak

from incy_arb import v4_replay
from incy_arb.cl_math import Q96, sqrt_at_tick
from incy_arb.cl_snapshot import BitmapTickMap, CLSnapshot
from incy_arb.flash_credit import POOL_MANAGER
from incy_arb.v4_replay import MODIFY, PROTOCOL, SWAP, apply_block, apply_v4_log, ordered_receipt_logs, restore_snapshot

ZERO = "0x" + "00" * 20
TOKEN = "0x" + "11" * 20
KEY = "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [(ZERO, TOKEN, 3000, 60, ZERO)])).hex()
H0, H1, TX = ("0x" + c * 64 for c in ("a", "b", "c"))


def snapshot():
    pool = {"address": KEY, "token0": ZERO, "token1": TOKEN, "hooks": ZERO, "fee_pips": 3000, "tick_spacing": 60}
    return CLSnapshot(
        "uniswap_v4",
        pool,
        100,
        H0,
        Q96,
        0,
        1000,
        60,
        3000,
        0,
        BitmapTickMap({-60: 1000, 60: -1000}, -15360, 30719, 60),
        {-60: 1000, 60: 1000},
    )


def event(topic, types, values):
    return {
        "address": POOL_MANAGER,
        "topics": ["0x" + topic.hex(), KEY] + (["0x" + "00" * 32] if topic in (MODIFY, SWAP) else []),
        "data": "0x" + encode(types, values).hex(),
    }


def modify(lower, upper, delta):
    return event(MODIFY, ["int24", "int24", "int256", "bytes32"], [lower, upper, delta, bytes(32)])


def swap(tick, liquidity=1000):
    return event(
        SWAP,
        ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
        [-10, 9, sqrt_at_tick(tick), liquidity, tick, 3000],
    )


def block():
    return {"number": "0x65", "hash": H1, "parentHash": H0, "transactions": [TX]}


def receipt(events):
    logs = []
    for i, e in enumerate(events):
        logs.append(
            dict(e, blockNumber="0x65", blockHash=H1, transactionHash=TX, transactionIndex="0x0", logIndex=hex(i))
        )
    return {
        "blockNumber": "0x65",
        "blockHash": H1,
        "transactionHash": TX,
        "transactionIndex": "0x0",
        "status": "0x1",
        "logs": logs,
    }


def test_liquidity_net_zero_tick_remains_initialized_until_gross_zero():
    original = snapshot()
    state, kind = apply_v4_log(original, modify(60, 120, 1000))
    assert kind == "liquidity" and state.ticks.net[60] == 0 and state.liquidity_gross[60] == 2000
    assert state.ticks.next_above(0) == 60 and state.liquidity == 1000
    state, _ = apply_v4_log(state, modify(-60, 60, -1000))
    assert -60 not in state.ticks.net and state.ticks.net[60] == 1000 and state.liquidity == 0
    state, _ = apply_v4_log(state, modify(60, 120, -1000))
    assert state.ticks.net == {} and state.liquidity_gross == {}
    assert original.ticks.net == {-60: 1000, 60: -1000} and original.liquidity == 1000


def test_outside_window_boundaries_still_update_active_liquidity():
    state, _ = apply_v4_log(snapshot(), modify(-60000, 60000, 250))
    assert state.liquidity == 1250 and state.ticks.net == snapshot().ticks.net


def test_zero_delta_does_not_initialize_ticks():
    before = snapshot()
    after, kind = apply_v4_log(before, modify(-120, 120, 0))
    assert before is after and kind == "liquidity_zero"


@pytest.mark.parametrize(
    "lower,upper,delta", [(-60, 60, -1001), (-61, 60, 1), (60, -60, 1), (-60, 60, 1 << 127), (-120, 120, -1)]
)
def test_invalid_liquidity_change_cannot_mutate_original(lower, upper, delta):
    before = snapshot()
    record = copy.deepcopy(before.record())
    with pytest.raises(ValueError):
        apply_v4_log(before, modify(lower, upper, delta))
    assert before.record() == record


def test_failed_later_event_rolls_back_whole_block():
    before = snapshot()
    with pytest.raises(ValueError):
        apply_block({KEY: before}, block(), [modify(-60, 60, 100), modify(-60, 60, -2000)])
    assert before.liquidity == 1000 and before.liquidity_gross[-60] == 1000


@pytest.mark.parametrize("changes", [{"number": "0x66"}, {"parentHash": H1}])
def test_block_gap_and_wrong_parent_fail(changes):
    with pytest.raises(ValueError, match="gap or parent"):
        apply_block({KEY: snapshot()}, dict(block(), **changes), [])


def test_swap_advances_price_and_outside_window_refuses_quotes():
    states, counts = apply_block({KEY: snapshot()}, block(), [swap(30000, 900)])
    state = states[KEY]
    assert (state.block, state.block_hash, state.tick, state.liquidity) == (101, H1, 30000, 900)
    assert counts == {"swap": 1}
    state, _ = apply_v4_log(state, swap(31000))
    assert state.quote(True, 1) is None and state.quote(False, 1) is None


def test_price_tick_boundary_and_directional_protocol_fee():
    before = snapshot()
    boundary = event(SWAP, ["int128", "int128", "uint160", "uint128", "int24", "uint24"], [-10, 9, Q96, 1000, -1, 3000])
    after, _ = apply_v4_log(before, boundary)
    assert after.tick == -1 and after.sqrt_price_x96 == Q96
    updated, kind = apply_v4_log(before, event(PROTOCOL, ["uint24"], [(100 << 12) + 500]))
    assert kind == "protocol_fee" and updated.fee(True) == 3499 and updated.fee(False) == 3100
    with pytest.raises(ValueError, match="protocol fee"):
        apply_v4_log(before, event(PROTOCOL, ["uint24"], [1001]))
    boundary["data"] = (
        "0x" + encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], [-10, 9, Q96, 1000, 3, 3000]).hex()
    )
    with pytest.raises(ValueError, match="price/tick/fee"):
        apply_v4_log(before, boundary)


def test_receipt_order_is_transaction_order_not_dictionary_or_rpc_list_order():
    first, second = TX, "0x" + "d" * 64
    b = dict(block(), transactions=[first, second])
    r1 = receipt([swap(1)])
    r2 = dict(receipt([]), transactionHash=second, transactionIndex="0x1")
    assert ordered_receipt_logs(b, [r2, r1]) == r1["logs"]


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "duplicate", "failed", "removed", "log_index", "tx_index", "block_hash", "log_hash", "log_tx"],
)
def test_incomplete_or_inconsistent_canonical_evidence_fails(mutation):
    r = receipt([swap(1)])
    receipts = [r]
    if mutation == "missing":
        receipts = []
    elif mutation == "extra":
        receipts.append(dict(r, transactionHash="0x" + "d" * 64))
    elif mutation == "duplicate":
        receipts.append(copy.deepcopy(r))
    elif mutation == "failed":
        r["status"] = "0x0"
    elif mutation == "removed":
        r["logs"][0]["removed"] = True
    elif mutation == "log_index":
        r["logs"][0]["logIndex"] = "0x1"
    elif mutation == "tx_index":
        r["transactionIndex"] = "0x1"
    elif mutation == "block_hash":
        r["blockHash"] = H0
    elif mutation == "log_hash":
        r["logs"][0]["blockHash"] = H0
    elif mutation == "log_tx":
        r["logs"][0]["transactionHash"] = "0x" + "d" * 64
    with pytest.raises(ValueError):
        ordered_receipt_logs(block(), receipts)


def test_snapshot_roundtrip_keeps_net_zero_and_gross():
    state, _ = apply_v4_log(snapshot(), modify(60, 120, 1000))
    assert restore_snapshot(state.record()).record() == state.record()
    record = state.record()
    record["tick_window"]["liquidity_gross"].pop("60")
    with pytest.raises(ValueError):
        restore_snapshot(record)


@pytest.mark.parametrize("mode", ["match", "difference", "reorg"])
def test_endpoint_is_independently_checked_and_reorg_aborts(monkeypatch, mode):
    initial = snapshot()

    class Rpc:
        def __init__(self, url):
            self.end_reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return 57073

        async def sealed_block(self, number):
            if number == 100:
                return {"hash": H0}
            self.end_reads += 1
            return dict(block(), hash=H0) if mode == "reorg" and self.end_reads > 1 else block()

        async def block_receipts(self, number):
            return [receipt([swap(1, 1200)])]

    async def load(rpc, venue, pool, number, words, window_tick):
        assert number == 101 and words == 1 and window_tick == 0
        state, _ = apply_v4_log(initial, swap(1, 1200 if mode == "match" else 1201))
        return replace(state, block=101, block_hash=H1)

    # Fixture uses one surrounding word: -15360 .. 30719 at spacing 60.
    monkeypatch.setattr(v4_replay, "ReadOnlyRpc", Rpc)
    monkeypatch.setattr(v4_replay, "load_cl_snapshot", load)
    scan = {"chain_id": 57073, "quote_block_number": 100, "quote_block_hash": H0, "snapshots": {KEY: initial.record()}}
    if mode == "reorg":
        with pytest.raises(RuntimeError, match="replaced"):
            asyncio.run(v4_replay.verify_v4_replay(scan, 1))
    else:
        result = asyncio.run(v4_replay.verify_v4_replay(scan, 1))
        assert result["summary"]["matches"] == int(mode == "match")
        assert result["summary"]["differences"] == int(mode == "difference")


@pytest.mark.parametrize("name", ["v4-replay-59201183", "v4-replay-59201002", "v4-replay-59100910"])
def test_captured_event_replay_matches_independent_stateview(name):
    from pathlib import Path

    report = v4_replay.read_scan(Path(__file__).resolve().parents[1] / "docs/validation" / (name + ".json.gz"))
    models = {key: restore_snapshot(record) for key, record in report["initial_snapshots"].items()}
    totals = {}
    for evidence in report["blocks"]:
        events = ordered_receipt_logs(evidence["block"], evidence["receipts"])
        models, counts = apply_block(models, evidence["block"], events)
        for kind, count in counts.items():
            totals[kind] = totals.get(kind, 0) + count
    assert totals == report["summary"]["applied_events"] and sum(totals.values()) > 0
    assert {key: s.record() for key, s in models.items()} == report["canonical_snapshots"]
