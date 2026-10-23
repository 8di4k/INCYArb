import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode

ROOT = Path(__file__).parents[1]
DATA = ROOT / "docs/validation/fresh-oracle-search"
spec = importlib.util.spec_from_file_location("fresh_search", ROOT / "scripts/search_flash_pairs.py")
search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(search)


def archive(name):
    return json.loads(gzip.decompress((DATA / name).read_bytes()))


@pytest.fixture
def best():
    data = archive("best-block-matrix.json.gz")
    row = max((r for r in data["rows"] if "assessment" in r), key=lambda r: int(r["assessment"]["net_eth_wei"]))
    return copy.deepcopy(row), data["header"]


def test_every_successful_matrix_budget_reproduces_without_turning_gross_into_net():
    for name, total, successful, positive in [
        ("initial-matrix.json.gz", 135, 16, 0),
        ("best-block-matrix.json.gz", 138, 16, 8),
    ]:
        data = archive(name)
        rows = [r for r in data["rows"] if "assessment" in r]
        assert len(data["rows"]) == total
        assert len(rows) == successful
        for row in rows:
            assert search.score(row, data["header"]) == row["assessment"]
        assert sum(r["assessment"]["status"] == "historical_estimated_positive" for r in rows) == positive


@pytest.mark.parametrize("tamper", ["route", "block", "gas", "fee_query", "bounded_call"])
def test_changed_route_or_execution_evidence_cannot_keep_a_positive_score(best, tamper):
    row, header = best
    if tamper == "route":
        row["amount_raw"] = str(int(row["amount_raw"]) - 1)
    elif tamper == "block":
        header["hash"] = "different canonical state"
    elif tamper == "gas":
        row["gas_limit"] -= 1
    elif tamper == "fee_query":
        row["fee_reads"]["getL1FeeUpperBound(uint256)"]["data"] = "0x1234"
    else:
        row["budget_call"]["transaction"]["maxFeePerGas"] = "0x1"
    with pytest.raises(ValueError):
        search.score(row, header)


def test_unknown_operator_fee_is_not_assumed_zero(best):
    row, header = best
    del row["fee_reads"]["getOperatorFee(uint256)"]["response"]
    assert search.score(row, header)["status"] == "fee_unavailable"


def test_loan_balance_must_be_restored(best):
    row, header = best
    values = list(decode(["uint256"] * 4, bytes.fromhex(row["response"][2:])))
    values[3] -= 1
    row["response"] = "0x" + encode(["uint256"] * 4, values).hex()
    with pytest.raises(ValueError, match="principal"):
        search.score(row, header)


def test_frontier_directions_cover_identical_states_and_positive_blocks_are_separate():
    forward = archive("frontier-forward.json.gz")
    reverse = archive("frontier-reverse.json.gz")
    assert len(forward["rows"]) == len(reverse["rows"]) == 128
    positives = []
    for data in (forward, reverse):
        assert len(data["canonical_rechecks"]) == 128
        previous = None
        for row, recheck in zip(data["rows"], data["canonical_rechecks"]):
            assert row["block"] == recheck["block"]
            assert row["header"]["hash"] == recheck["hash"]
            if previous:
                assert row["header"]["parentHash"] == previous["hash"]
            previous = row["header"]
        positives.append(
            [
                r["block"]
                for r in data["rows"]
                if r.get("assessment", {}).get("status") == "historical_estimated_positive"
            ]
        )
    assert [(r["block"], r["block_hash"]) for r in forward["rows"]] == [
        (r["block"], r["block_hash"]) for r in reverse["rows"]
    ]
    assert positives == [[52962834, 52962853], []]
    selected = [r for r in forward["rows"] if r["block"] in positives[0]]
    assert int(selected[1]["header"]["timestamp"], 16) - int(selected[0]["header"]["timestamp"], 16) == 38


def test_cap_refinement_has_no_linear_extrapolation_beyond_the_actual_fill_limit():
    data = archive("best-sizing.json.gz")
    over = [r for r in data["rows"] if int(r["amount_raw"]) > 63350000000000000]
    assert len(over) == 2
    for row in over:
        raw = bytes.fromhex(row["revert_data"][2:])
        assert raw[:4] == search.keccak(text="LegFailed(uint256,uint256,bytes)")[:4]
        _, stage, nested = decode(["uint256", "uint256", "bytes"], raw[4:])
        assert stage == 2
        assert nested[:4].hex() == "51c3dffc"
        cap, cumulative = decode(["uint256", "uint256"], nested[4:])
        assert cap == 63350000000000000 < cumulative


def test_best_protected_trade_meets_a_profit_floor_above_its_network_budget():
    data = archive("minimum-profit-verification.json.gz")
    row, rejected = data["rows"]
    assert search.base.net_weth(row, data["header"])["net_eth_wei"] == "5357883005626"
    _, minimum = decode(["(uint256,uint256,bool,address)", "uint256"], bytes.fromhex(row["transaction"]["data"][10:]))
    assert int(row["profit_raw"]) >= minimum > int(row["assessment"]["total_fee_budget_wei"])
    assert minimum == 6000000000000
    raw = bytes.fromhex(rejected["revert_data"][2:])
    assert raw[:4] == search.keccak(text="InsufficientProfit(uint256,uint256)")[:4]
    actual, demanded = decode(["uint256", "uint256"], raw[4:])
    assert actual == int(row["profit_raw"]) < demanded


def test_source_and_evidence_manifest_hashes_match():
    manifest = json.loads((DATA / "manifest.json").read_text())
    for record in manifest["files"] + manifest["source_dependencies"]:
        raw = (DATA / record["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["sha256"]
    for name in ("initial-matrix.json.gz", "best-block-matrix.json.gz", "frontier-forward.json.gz"):
        overrides = archive(name)["overrides"]
        assert not ({search.base.WETH, search.base.USDC, search.BTC, search.base.MANAGER} & set(overrides))
        for value in overrides.values():
            assert set(value) == ({"code"} if "code" in value else {"balance"})
