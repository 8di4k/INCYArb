import copy
import gzip
import importlib.util
import json
from pathlib import Path

import pytest
from eth_abi import decode, encode

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("flash_pair_probe", ROOT / "scripts/probe_flash_pair.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def example():
    archive = json.loads(
        gzip.decompress((ROOT / "docs/validation/flash-funded-oracle/best-reproduction.json.gz").read_bytes())
    )
    row = copy.deepcopy(archive["rows"][0])
    return row, row["header"]


def test_archived_financed_circle_is_positive_after_repayment_payout_and_all_fees(example):
    row, header = example
    result = probe.net_weth(row, header)
    assert result["net_eth_wei"] == "1585098067598"
    assert result["total_fee_budget_wei"] == "3994684827497"
    assert result["status"] == "historical_estimated_positive"
    assert row["manager_before"] == row["manager_after"]
    assert int(row["profit_raw"]) < int(row["amount_raw"])


@pytest.mark.parametrize("change", ["principal", "rounds", "profit"])
def test_repayment_and_round_changes_must_reconcile(example, change):
    row, _ = example
    profit, before, after, changes = decode(
        ["uint256", "uint256", "uint256", "int256[]"], bytes.fromhex(row["response"][2:])
    )
    if change == "principal":
        after -= 1
    elif change == "rounds":
        changes = ()
    else:
        profit += 1
    raw = "0x" + encode(["uint256", "uint256", "uint256", "int256[]"], [profit, before, after, changes]).hex()
    with pytest.raises(ValueError, match="reconcile"):
        probe.financed_result(raw, int(row["amount_raw"]), 1)


def test_l1_and_operator_fees_are_each_counted_once(example):
    row, header = example
    operator = row["fee_reads"]["getOperatorFee(uint256)"]
    operator["response"] = "0x" + encode(["uint256"], [1000]).hex()
    assert probe.net_weth(row, header)["net_eth_wei"] == str(1585098067598 - 1000)


def test_missing_operator_fee_is_unknown_instead_of_zero(example):
    row, header = example
    del row["fee_reads"]["getOperatorFee(uint256)"]["response"]
    assert probe.net_weth(row, header)["status"] == "fee_unavailable"


def test_lower_content_fee_cannot_promote_a_negative_upper_budget(example):
    row, header = example
    row["fee_reads"]["getL1FeeUpperBound(uint256)"]["response"] = (
        "0x" + encode(["uint256"], [int(row["profit_raw"])]).hex()
    )
    assert probe.net_weth(row, header)["status"] == "not_positive_after_budget"


@pytest.mark.parametrize("tamper", ["block", "gas", "calldata", "fee_read", "decoded_profit", "gas_execution"])
def test_changed_evidence_is_rejected(example, tamper):
    row, header = example
    if tamper == "block":
        header["hash"] = "different block"
    elif tamper == "gas":
        row["gas_limit"] -= 1
    elif tamper == "calldata":
        row["transaction"]["data"] += "00"
    elif tamper == "fee_read":
        row["fee_reads"]["getL1FeeUpperBound(uint256)"]["data"] = "0x1234"
    elif tamper == "gas_execution":
        row["budget_call"]["transaction"]["gas"] = hex(row["gas_limit"] + 1)
    else:
        row["profit_raw"] = str(int(row["profit_raw"]) + 1)
    with pytest.raises(ValueError):
        probe.net_weth(row, header)


def test_fee_envelope_uses_base_chain_and_the_simulated_tip(example):
    row, header = example
    raw = bytes.fromhex(row["unsigned_transaction"][2:])
    import rlp

    fields = rlp.decode(raw[1:])
    assert int.from_bytes(fields[0]) == 8453
    row["priority_fee_wei"] = "10000000"
    with pytest.raises(ValueError, match="gas price"):
        probe.net_weth(row, header)


def test_whole_window_preserves_canonical_coverage_and_reconciles_every_successful_budget():
    archive = json.loads(gzip.decompress((ROOT / "docs/validation/flash-funded-oracle/window.json.gz").read_bytes()))
    assert len(archive["rows"]) == len(archive["canonical_rechecks"]) == 300
    assert [r["block"] for r in archive["rows"]] == list(range(52960000, 52960300))
    assert sum("negative_gross_raw" in r for r in archive["rows"]) == 285
    assert sum("profit_raw" in r for r in archive["rows"]) == 15
    previous = None
    positives = []
    for row, check in zip(archive["rows"], archive["canonical_rechecks"]):
        header = row["header"]
        assert row["block"] == check["block"]
        assert header["hash"] == check["hash"]
        if previous:
            assert header["parentHash"] == previous["hash"]
        previous = header
        if "profit_raw" in row:
            assert probe.net_weth(row, header) == row["assessment"]
            if row["assessment"]["status"] == "historical_estimated_positive":
                positives.append(row["block"])
        else:
            assert int(row["negative_gross_raw"]) < 0
    assert positives == [52960086, 52960087]
    # Adjacent independent snapshots are not certified separate executions.
    assert "independently" in archive["selection"]
    assert not ({probe.USDC, probe.WETH, probe.MANAGER} & set(archive["overrides"]))


def test_archived_execution_controls_reject_exhausted_depth_and_excessive_profit_floor():
    archive = json.loads(
        gzip.decompress((ROOT / "docs/validation/flash-funded-oracle/controls-and-fees.json.gz").read_bytes())
    )
    exhausted = next(r for r in archive["rows"] if r["rounds"] == 2 and r["amount_raw"] == "63350000000000000")
    raw = bytes.fromhex(exhausted["revert_data"][2:])
    assert raw[:4] == probe.keccak(text="LegFailed(uint256,uint256,bytes)")[:4]
    round_index, stage, nested = decode(["uint256", "uint256", "bytes"], raw[4:])
    assert (round_index, stage) == (1, 2)
    # The direct executor has a different error ABI from venue Inactive().
    # Preserve its observed selector and cap/cumulative-size words without
    # guessing a Solidity error name from a signature database.
    assert nested[:4].hex() == "51c3dffc"
    assert decode(["uint256", "uint256"], nested[4:]) == (63350000000000000, 126700000000000000)
    protected = next(r for r in archive["rows"] if int(r["minimum_profit_raw"]) > 0)
    raw = bytes.fromhex(protected["revert_data"][2:])
    assert raw[:4] == probe.keccak(text="InsufficientProfit(uint256,uint256)")[:4]
    actual, minimum = decode(["uint256", "uint256"], raw[4:])
    assert 0 < actual < minimum
