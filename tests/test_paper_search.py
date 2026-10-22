import copy
import json
from pathlib import Path

import pytest

from incy_arb.local_scan import size_local_paths
from incy_arb.paper_search import paper_search
from incy_arb.subblock_replay import captured_payload, replay_payload
from incy_arb.v4_replay import read_scan, restore_snapshot
from incy_arb.research import cycle_paths

ROOT = Path(__file__).resolve().parents[1] / "docs/validation"


@pytest.fixture
def inputs():
    return read_scan(ROOT / "subblock-search-baseline-59203420.json.gz"), read_scan(
        ROOT / "subblock-v4-replay-59203420.json.gz"
    )


def test_incremental_search_tracks_short_lived_candidates_and_matches_independent_evm(inputs):
    baseline, replay = inputs
    original = copy.deepcopy(baseline)
    result = paper_search(baseline, replay)
    assert baseline == original
    summary = result["summary"]
    assert (summary["pools"], summary["cycles"], summary["subblocks"]) == (44, 128, 240)
    assert summary["incremental_searches"] == 2 and summary["cycles_recomputed"] == 44
    assert summary["initial_positive_gross"] == 8 and summary["final_positive_gross"] == 9
    assert result["changes"][0]["new_candidates"] == 2 and result["changes"][1]["removed_candidates"] == 1
    simulation = json.loads((ROOT / "paper-peak-evm-59203443.json").read_text())
    assert summary["peak_gross_pnl"] == simulation["simulated_gross_pnl"] == "10809806094"
    assert summary["peak_cursor"] == {"block_number": simulation["block_number"], "subblock_index": 5}
    assert simulation["status"] == "success" and simulation["quote_difference"] == "0"
    assert simulation["net_profit_assessment"]["status"] == "estimated_positive"
    # EVM validation belongs only to the separately saved complete sealed block.
    assert "net_profit_assessment" not in result and "execution_validation" not in result


def test_affected_cycle_updates_equal_full_recomputation_after_each_swap(inputs):
    from incy_arb.local_scan import LocalV4Config

    baseline, replay = inputs
    result = paper_search(baseline, replay)
    models = {key: restore_snapshot(r) for key, r in baseline["snapshots"].items()}
    paths = cycle_paths([(s.venue, s.pool) for s in models.values()], tuple(baseline["start_tokens"]), 3)
    config = LocalV4Config(Path("unused"), Path("unused"), 10**9, 10**17)
    cursors = {(c["block_number"], c["subblock_index"]): c for c in result["changes"]}
    compared = 0
    for evidence in replay["blocks"]:
        payload = captured_payload(evidence["capture"])
        final, prefixes = replay_payload(models, payload, evidence["block"], evidence["receipts"])
        for prefix in prefixes:
            for key, r in prefix["pricing_state_after"].items():
                models[key] = restore_snapshot(
                    dict(r, block_number=models[key].block, block_hash=models[key].block_hash)
                )
            cursor = (payload.block_number, prefix["subblock_index"])
            if cursor in cursors:
                fresh = size_local_paths(paths, models, config)
                assert fresh["findings"] == cursors[cursor]["findings"]
                compared += 1
        models = final
    assert compared == 2


@pytest.mark.parametrize(
    "change",
    [
        "chain",
        "start",
        "hash",
        "missing_pool",
        "initial_price",
        "final_price",
        "endpoint_set",
        "gap",
        "duplicate",
        "endpoint",
        "steps",
        "grid",
    ],
)
def test_inconsistent_evidence_is_rejected_instead_of_emitting_paper_candidates(inputs, change):
    baseline, replay = inputs
    tracked = next(iter(replay["initial_snapshots"]))
    if change == "chain":
        baseline["chain_id"] = 1
    elif change == "start":
        baseline["quote_block_number"] -= 1
    elif change == "hash":
        replay["initial_snapshots"][tracked]["block_hash"] = "0xdead"
    elif change == "missing_pool":
        baseline["snapshots"].pop(tracked)
    elif change == "initial_price":
        replay["initial_snapshots"][tracked]["liquidity"] = "1"
    elif change == "final_price":
        replay["canonical_snapshots"][tracked]["liquidity"] = "1"
    elif change == "endpoint_set":
        replay["canonical_snapshots"] = {}
    elif change == "gap":
        replay["blocks"].pop(1)
    elif change == "duplicate":
        replay["blocks"].insert(1, replay["blocks"][0])
    elif change == "endpoint":
        replay["end_block"] += 1
    elif change == "steps":
        baseline["sizing_options"]["max_steps"] = 8
    elif change == "grid":
        baseline["sizing_options"]["grid_points"] = 1000000
    with pytest.raises(ValueError):
        paper_search(baseline, replay)
