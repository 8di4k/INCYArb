from decimal import Decimal

import pytest

from incy_arb.competitor_cashflows import (
    DEPOSIT,
    ETH_EQUIVALENT,
    STABLES,
    TRANSFER,
    WETH,
    WITHDRAWAL,
    analyze_day,
    equivalent_vector,
    native_delta,
    price_component,
    receipt_vector,
)

CALLER, EXECUTOR, POOL, VENUE, PAYOUT, OTHER = ("0x" + f"{i:02x}" * 20 for i in range(1, 7))
USD = next(iter(STABLES))
HASH = "0x" + "ab" * 32
ZERO = "0x" + "00" * 20


def transfer(index, token, sender, recipient, amount):
    return {
        "transactionHash": HASH,
        "logIndex": hex(index),
        "address": token,
        "topics": [TRANSFER, "0x" + sender[2:].rjust(64, "0"), "0x" + recipient[2:].rjust(64, "0")],
        "data": "0x" + amount.to_bytes(32, "big").hex(),
    }


def native(index, sender, recipient, amount, kind="call", success=True):
    return {
        "transaction_hash": HASH,
        "index": index,
        "from": {"hash": sender},
        "to": {"hash": recipient},
        "value": str(amount),
        "type": kind,
        "success": success,
    }


def receipt(logs=(), success=True):
    return {
        "transactionHash": HASH,
        "from": CALLER,
        "to": EXECUTOR,
        "blockNumber": "0xa",
        "status": "0x1" if success else "0x0",
        "logs": list(logs),
        "gasUsed": "0xa",
        "effectiveGasPrice": "0x2",
        "l1Fee": "0x5",
    }


def day(r):
    return {
        "chain_id": 57073,
        "eth_usd_reference": "2400",
        "window": {},
        "native_transfers": [],
        "transactions": {
            HASH: {
                "from": {"hash": CALLER},
                "to": {"hash": EXECUTOR},
                "block_number": 10,
                "timestamp": "2026-10-22T00:00:00Z",
                "status": "ok" if int(r["status"], 16) else "error",
                "value": "0",
                "fee": {"type": "actual", "value": "25"},
            }
        },
        "receipts": {HASH: r},
        "beneficiaries": {EXECUTOR: PAYOUT},
    }


def test_flash_principal_and_internal_payout_do_not_inflate_surplus():
    logs = [
        transfer(i, USD, fr, to, amount)
        for i, (fr, to, amount) in enumerate(
            [
                (POOL, EXECUTOR, 100),
                (EXECUTOR, VENUE, 100),
                (VENUE, EXECUTOR, 101),
                (EXECUTOR, POOL, 100),
                (EXECUTOR, PAYOUT, 1),
            ]
        )
    ]
    result = analyze_day(day(receipt(logs)))
    assert result["transactions"][0]["pre_fee_vector"] == {USD: "1"}
    assert result["ownership_verified"] is False


def test_direct_pool_payment_to_sender_is_visible_even_without_executor_custody():
    assert receipt_vector(receipt([transfer(0, USD, POOL, CALLER, 3)]), [], {CALLER, EXECUTOR})[USD] == 3


def test_unwrap_and_native_loan_repayment_are_not_profit():
    logs = [transfer(0, WETH, POOL, EXECUTOR, 100), transfer(1, WETH, EXECUTOR, ZERO, 100)]
    moves = [
        native(1, POOL, EXECUTOR, 100),
        native(2, EXECUTOR, VENUE, 100),
        native(3, WETH, EXECUTOR, 100),
        native(4, EXECUTOR, POOL, 100),
    ]
    # Loan +100 native -> external venue -100; WETH proceeds unwrap and repay the native debt.
    assert equivalent_vector(receipt_vector(receipt(logs), moves, {CALLER, EXECUTOR})) == {}


@pytest.mark.parametrize("event,sign", [(DEPOSIT, 1), (WITHDRAWAL, -1)])
def test_weth9_supply_changes_without_transfer_events_cancel_native_leg(event, sign):
    log = {
        "transactionHash": HASH,
        "logIndex": "0x0",
        "address": WETH,
        "topics": [event, "0x" + EXECUTOR[2:].rjust(64, "0")],
        "data": "0x" + (100).to_bytes(32, "big").hex(),
    }
    movement = native(1, EXECUTOR, WETH, 100) if sign == 1 else native(1, WETH, EXECUTOR, 100)
    assert equivalent_vector(receipt_vector(receipt([log]), [movement], {CALLER, EXECUTOR})) == {}


def test_open_asset_purchase_keeps_negative_start_asset_component():
    logs = [transfer(0, OTHER, POOL, CALLER, 100)]
    vector = receipt_vector(receipt(logs), [native(1, EXECUTOR, POOL, 100)], {CALLER, EXECUTOR}, 100)
    assert equivalent_vector(vector) == {OTHER: 100, ETH_EQUIVALENT: -100}


def test_delegatecall_inherited_value_and_failed_calls_do_not_move_eth():
    calls = [native(1, POOL, EXECUTOR, 100, "delegatecall"), native(2, POOL, EXECUTOR, 100, success=False)]
    assert native_delta(calls, {EXECUTOR}) == 0


def test_failed_root_ignores_successful_children_and_still_pays_network_fee():
    data = day(receipt(success=False))
    data["native_transfers"] = [native(1, POOL, CALLER, 1000)]
    row = analyze_day(data)["transactions"][0]
    assert row["pre_fee_vector"] == {}
    assert row["network_fee_wei"] == "25"
    assert Decimal(row["marked_component_after_fee_usd"]) == Decimal(-25) / 10**18 * 2400


def test_l1_is_reconciled_once_and_unexplained_fee_difference_blocks_accounting():
    data = day(receipt())
    group = analyze_day(data)["by_sender"][CALLER]
    assert group["network_fee_wei"] == "25" and group["l1_fee_wei"] == "5"
    data["transactions"][HASH]["fee"]["value"] = "30"
    with pytest.raises(ValueError, match="reconciliation"):
        analyze_day(data)


def test_negative_unpriced_inventory_is_retained_and_never_silently_marked_as_dollars():
    data = day(receipt([transfer(0, OTHER, CALLER, POOL, 100)]))
    row = analyze_day(data)["transactions"][0]
    assert row["unpriced_components"] == {OTHER: "-100"}
    assert row["has_negative_asset_change_before_fee"]
    assert price_component({OTHER: 10**30}, "2400") == 0


def test_eth_equivalence_preserves_an_already_combined_balance():
    combined = equivalent_vector({WETH: 100, "native:eth": -20, ETH_EQUIVALENT: 10})
    assert combined == {ETH_EQUIVALENT: 90}
    assert equivalent_vector(combined) == combined


def test_missing_receipt_and_duplicate_native_index_are_rejected():
    data = day(receipt())
    data["receipts"] = {}
    with pytest.raises(ValueError, match="one RPC receipt"):
        analyze_day(data)
    move = native(1, POOL, CALLER, 10)
    with pytest.raises(ValueError, match="duplicate"):
        native_delta([move, move], {CALLER})


@pytest.mark.parametrize("incomplete", ["receipts", "native"])
def test_explicitly_incomplete_collection_cannot_become_a_day_result(incomplete):
    data = day(receipt())
    if incomplete == "receipts":
        data["complete_receipts"] = False
    else:
        data["native_coverage"] = [{"address": EXECUTOR, "complete": True}]
    with pytest.raises(ValueError, match="incomplete|missing a participant"):
        analyze_day(data)
