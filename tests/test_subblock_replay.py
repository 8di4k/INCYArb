import copy
import json
from dataclasses import replace

import pytest
from eth_abi import encode
from eth_utils import keccak

from incy_arb.cl_math import sqrt_at_tick
from incy_arb.subblock_replay import captured_payload, read_capture, replay_payload
from incy_arb.subblocks import PayloadCollector
from incy_arb.v4_replay import SWAP, restore_snapshot
from incy_arb.flash_credit import POOL_MANAGER

H0, H1 = "0x" + "aa" * 32, "0x" + "bb" * 32
KEY = "0x" + "11" * 32


def data():
    raw1, raw2 = "0x0201", "0x0202"
    hashes = ["0x" + keccak(bytes.fromhex(r[2:])).hex() for r in (raw1, raw2)]
    base = {"block_number": 101, "parent_hash": H0, "timestamp": 123, "base_fee_per_gas": 5}
    messages, receipts = [], []
    for i, (raw, tx) in enumerate(zip((raw1, raw2), hashes)):
        event = {
            "address": POOL_MANAGER,
            "topics": ["0x" + SWAP.hex(), KEY, "0x" + "00" * 32],
            "data": "0x"
            + encode(
                ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                [-10, 9, sqrt_at_tick(i + 1), 1000, i + 1, 3000],
            ).hex(),
        }
        source = {"status": "0x1", "type": "0x2", "cumulativeGasUsed": hex((i + 1) * 21000), "logs": [event]}
        message = {
            "payload_id": "p",
            "index": i,
            "diff": {"transactions": [raw]},
            "metadata": {"block_number": 101, "receipts": {tx: source}},
        }
        if i == 0:
            message["base"] = base
        messages.append(message)
        canonical = dict(
            source,
            transactionHash=tx,
            transactionIndex=hex(i),
            blockNumber=hex(101),
            blockHash=H1,
            logs=[
                dict(
                    event,
                    transactionHash=tx,
                    transactionIndex=hex(i),
                    blockNumber=hex(101),
                    blockHash=H1,
                    logIndex=hex(i),
                )
            ],
        )
        receipts.append(canonical)
    block = {
        "number": hex(101),
        "hash": H1,
        "parentHash": H0,
        "timestamp": hex(123),
        "baseFeePerGas": hex(5),
        "transactions": hashes,
    }
    payload = PayloadCollector._start(messages[0])
    payload.add(messages[1])
    snapshot = restore_snapshot(
        {
            "venue": "uniswap_v4",
            "pool": {"address": KEY},
            "block_number": 100,
            "block_hash": H0,
            "sqrt_price_x96": str(sqrt_at_tick(0)),
            "tick": 0,
            "liquidity": "1000",
            "tick_spacing": 60,
            "lp_fee": 3000,
            "protocol_fee": 0,
            "tick_window": {"lo": -15360, "hi": 15359, "liquidity_net": {}, "liquidity_gross": {}},
        }
    )
    return {KEY: snapshot}, payload, block, receipts


def test_subblock_boundaries_preserve_each_prefix_and_final_state():
    states, payload, block, receipts = data()
    final, prefixes = replay_payload(states, payload, block, list(reversed(receipts)))
    assert [p["pricing_state_after"][KEY]["tick"] for p in prefixes] == [1, 2]
    assert [p["subblock_index"] for p in prefixes] == [0, 1]
    assert "block_number" not in prefixes[0]["pricing_state_after"][KEY]
    assert final[KEY].tick == 2 and final[KEY].block == 101 and final[KEY].block_hash == H1
    assert states[KEY].tick == 0 and states[KEY].block == 100


def test_receipt_map_order_does_not_change_raw_transaction_order():
    states, payload, block, receipts = data()
    first, second = payload.messages
    first["diff"]["transactions"] += second["diff"]["transactions"]
    first["metadata"]["receipts"] = dict(
        list(second["metadata"]["receipts"].items()) + list(first["metadata"]["receipts"].items())
    )
    first["index"] = 0
    merged = PayloadCollector._start(first)
    final, prefixes = replay_payload(states, merged, block, receipts)
    assert final[KEY].tick == 2 and len(prefixes) == 1 and prefixes[0]["transactions"] == 2


@pytest.mark.parametrize("change", ["source_log", "canonical_order", "gap", "wrong_parent", "later_bad_event"])
def test_invalid_capture_cannot_partially_advance_state(change):
    states, payload, block, receipts = data()
    if change == "source_log":
        payload.transactions[-1].receipt["logs"][0]["data"] = "0x"
    elif change == "canonical_order":
        receipts[1]["logs"][0]["logIndex"] = "0x0"
    elif change == "gap":
        states = {KEY: replace(states[KEY], block=99)}
    elif change == "wrong_parent":
        block["parentHash"] = H1
    elif change == "later_bad_event":
        # Matching canonical evidence can still contain a pricing event outside the supported model.
        bad = (
            "0x"
            + encode(
                ["int128", "int128", "uint160", "uint128", "int24", "uint24"], [-10, 9, sqrt_at_tick(2), 1000, 2, 4000]
            ).hex()
        )
        payload.transactions[-1].receipt["logs"][0]["data"] = bad
        receipts[-1]["logs"][0]["data"] = bad
    before = copy.deepcopy(states[KEY].record())
    with pytest.raises(ValueError):
        replay_payload(states, payload, block, receipts)
    assert states[KEY].record() == before


def test_source_record_flags_do_not_replace_rebuilding_raw_payload():
    _, payload, _, _ = data()
    record = payload.record()
    assert captured_payload(record).valid
    record["messages"][1]["index"] = 3
    record["valid"] = True
    with pytest.raises(ValueError):
        captured_payload(record)


def test_capture_completion_order_is_sorted_but_gaps_and_duplicates_fail(tmp_path):
    path = tmp_path / "capture.jsonl"
    path.write_text("\n".join(json.dumps({"block_number": n}) for n in (102, 100, 101)))
    assert [r["block_number"] for r in read_capture(path)] == [100, 101, 102]
    for numbers in ((100, 102), (100, 100), ()):
        path.write_text("\n".join(json.dumps({"block_number": n}) for n in numbers))
        with pytest.raises(ValueError):
            read_capture(path)


def test_pool_selection_reports_hooks_unknown_keys_and_new_initializations():
    from incy_arb.subblock_replay import tracked_pools

    _, payload, _, _ = data()
    definition = {
        "currency0": "0x" + "00" * 20,
        "currency1": "0x" + "22" * 20,
        "fee_pips": 3000,
        "tick_spacing": 60,
        "hooks": "0x" + "00" * 20,
        "source": {"block_number": 99},
    }
    discovery = {"chain_id": 57073, "pool_manager": POOL_MANAGER, "pools": {KEY: definition}}
    records = [payload.record()]
    pools, excluded = tracked_pools(records, discovery)
    assert set(pools) == {KEY} and excluded == {}
    definition["hooks"] = "0x" + "01" * 20
    assert tracked_pools(records, discovery)[1] == {KEY: "hooks_or_unsupported_fee"}
    definition["hooks"] = "0x" + "00" * 20
    definition["source"]["block_number"] = 101
    assert tracked_pools(records, discovery)[1] == {KEY: "initialized_during_capture"}
    discovery["pools"] = {}
    assert tracked_pools(records, discovery)[1] == {KEY: "missing_discovery_key"}


@pytest.mark.parametrize("mode", ["match", "wrong_chain", "reorg"])
def test_subblock_cli_rechecks_chain_and_canonical_hashes(tmp_path, monkeypatch, mode):
    import asyncio
    from incy_arb import subblock_replay

    initial, payload, block, receipts = data()
    capture = tmp_path / "capture.jsonl"
    capture.write_text(json.dumps(payload.record()) + "\n")
    discovery = tmp_path / "discovery.json"
    discovery.write_text(
        json.dumps(
            {
                "chain_id": 57073,
                "pool_manager": POOL_MANAGER,
                "pools": {
                    KEY: {
                        "currency0": "0x" + "00" * 20,
                        "currency1": "0x" + "22" * 20,
                        "fee_pips": 3000,
                        "tick_spacing": 60,
                        "hooks": "0x" + "00" * 20,
                        "source": {"block_number": 99},
                    }
                },
            }
        )
    )

    class Rpc:
        def __init__(self, url):
            self.end_reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return 1 if mode == "wrong_chain" else 57073

        async def sealed_block(self, number):
            if number == 100:
                return {"hash": H0}
            self.end_reads += 1
            return dict(block, hash=H0) if mode == "reorg" and self.end_reads > 1 else block

        async def block_receipts(self, number):
            return receipts

    async def load(*args):
        return initial[KEY]

    async def compare(rpc, baseline, final):
        assert final[KEY].tick == 2
        return {KEY: {"status": "match"}}, {KEY: final[KEY].record()}

    monkeypatch.setattr(subblock_replay, "ReadOnlyRpc", Rpc)
    monkeypatch.setattr(subblock_replay, "load_cl_snapshot", load)
    monkeypatch.setattr(subblock_replay, "compare_endpoint", compare)
    if mode == "match":
        report = asyncio.run(subblock_replay.verify_subblock_replay(capture, discovery))
        assert report["summary"] == {
            "blocks": 1,
            "subblocks": 2,
            "pools": 1,
            "applied_events": {"swap": 2},
            "matches": 1,
            "differences": 0,
        }
    else:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(subblock_replay.verify_subblock_replay(capture, discovery))


def test_canonical_capture_replays_all_subblock_boundaries_offline():
    import gzip
    from pathlib import Path

    artifact = Path(__file__).resolve().parents[1] / "docs/validation/subblock-v4-replay-59203420.json.gz"
    report = json.loads(gzip.decompress(artifact.read_bytes()))
    states = {key: restore_snapshot(r) for key, r in report["initial_snapshots"].items()}
    boundaries = swaps = 0
    for evidence in report["blocks"]:
        payload = captured_payload(evidence["capture"])
        states, prefixes = replay_payload(states, payload, evidence["block"], evidence["receipts"])
        assert prefixes == evidence["prefixes"]
        boundaries += len(prefixes)
        swaps += sum(p["applied_events"].get("swap", 0) for p in prefixes)
    assert boundaries == 240 and swaps == 2
    assert {key: s.record() for key, s in states.items()} == report["canonical_snapshots"]
