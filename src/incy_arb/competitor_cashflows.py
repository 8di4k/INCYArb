"""Explicit-address cashflow accounting; never infer bot ownership or trading profit.

Inputs are historical receipts plus successful native value transfers from an
explorer index. An address boundary is a research hypothesis. Position changes,
unknown tokens, missing native coverage and external ownership still matter.
"""

from collections import Counter, defaultdict
from decimal import Decimal

from eth_utils import keccak

TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
DEPOSIT = "0x" + keccak(text="Deposit(address,uint256)").hex()
WITHDRAWAL = "0x" + keccak(text="Withdrawal(address,uint256)").hex()
WETH = "0x4200000000000000000000000000000000000006"
ETH = "native:eth"
ETH_EQUIVALENT = "eth+weth"
STABLES = {
    "0x2d270e6886d130d724215a266106e6832161eaed": "USDC",
    "0x0200c29006150606b650577bbe7b6248f58470c1": "USDt0",
    "0xe343167631d89b6ffc58b88d6b7fb0228795491d": "USDG",
    "0xf1815bd50389c46847f0bda824ec8da914045d14": "USDC.e",
}
SWAPS = {
    "0x" + keccak(text=signature).hex()
    for signature in (
        "Swap(address,uint256,uint256,uint256,uint256,address)",
        "Swap(address,address,int256,int256,uint160,uint128,int24)",
        "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
    )
}


def _address(value):
    return value.get("hash", "").lower() if isinstance(value, dict) else (value or "").lower()


def native_delta(transfers, boundary):
    """Delegatecall's inherited value is not an ETH transfer. Failed roots are handled upstream."""
    total, seen = 0, set()
    for transfer in transfers:
        key = (transfer["transaction_hash"], transfer["index"])
        if key in seen:
            raise ValueError("duplicate native trace index")
        seen.add(key)
        if not transfer.get("success") or transfer.get("error"):
            continue
        if transfer["type"].lower() not in {"call", "create", "create2", "selfdestruct", "suicide"}:
            continue
        sender = _address(transfer["from"])
        recipient = _address(transfer.get("to") or transfer.get("created_contract"))
        value = int(transfer["value"])
        total += value * ((recipient in boundary) - (sender in boundary))
    return total


def receipt_vector(receipt, native_transfers, boundary, root_value=0):
    """Signed asset changes across an explicit boundary, before transaction network fees."""
    boundary = {address.lower() for address in boundary}
    vector = Counter()
    if int(receipt["status"], 16) == 0:
        return vector
    seen = set()
    for log in receipt["logs"]:
        key = (log["transactionHash"], log["logIndex"])
        if key in seen:
            raise ValueError("duplicate receipt log index")
        seen.add(key)
        topics = log.get("topics", [])
        # OP Stack WETH9 emits Deposit/Withdrawal rather than ERC-20 mint/burn
        # Transfer events. Ignoring these fabricates profit from repaid loans.
        if (
            log["address"].lower() == WETH
            and len(topics) == 2
            and topics[0].lower() in {DEPOSIT, WITHDRAWAL}
            and len(log.get("data", "")) == 66
        ):
            if len(topics[1]) != 66 or int(topics[1][2:26], 16):
                raise ValueError("noncanonical WETH9 event account")
            account = "0x" + topics[1][-40:].lower()
            if account in boundary:
                sign = 1 if topics[0].lower() == DEPOSIT else -1
                vector[WETH] += sign * int(log["data"], 16)
            continue
        # ERC-721 Transfer has four topics; malformed/nonstandard logs are not ERC-20 amounts.
        if len(topics) != 3 or topics[0].lower() != TRANSFER or len(log.get("data", "")) != 66:
            continue
        if any(len(topic) != 66 or int(topic[2:26], 16) for topic in topics[1:]):
            raise ValueError("noncanonical indexed address")
        sender, recipient = ("0x" + topic[-40:].lower() for topic in topics[1:])
        vector[log["address"].lower()] += int(log["data"], 16) * ((recipient in boundary) - (sender in boundary))
    vector[ETH] += native_delta(native_transfers, boundary)
    vector[ETH] += int(root_value) * ((_address(receipt["to"]) in boundary) - (_address(receipt["from"]) in boundary))
    return vector


def equivalent_vector(vector):
    combined = Counter(vector)
    combined[ETH_EQUIVALENT] += combined.pop(ETH, 0) + combined.pop(WETH, 0)
    return {token: value for token, value in combined.items() if value}


def price_component(vector, eth_usd):
    """Scenario mark: named stablecoins at $1; ETH/WETH at the supplied reference.

    Omitted tokens may have either sign. This is not a complete P/L valuation.
    """
    vector = equivalent_vector(vector)
    stable = sum(Decimal(vector.get(token, 0)) / 10**6 for token in STABLES)
    native = Decimal(vector.get(ETH_EQUIVALENT, 0)) / 10**18 * Decimal(eth_usd)
    return stable + native


def analyze_day(data):
    if data["chain_id"] != 57073:
        raise ValueError("Ink evidence required")
    if data.get("complete_receipts") is False:
        raise ValueError("receipt collection is incomplete")
    for category in ("history_coverage", "native_coverage"):
        if category in data and any(not row.get("complete") for row in data[category]):
            raise ValueError(f"incomplete {category}")
    transactions, receipts = data["transactions"], data["receipts"]
    if set(transactions) != set(receipts):
        raise ValueError("one RPC receipt per scoped transaction required")
    native = defaultdict(list)
    for record in data["native_transfers"]:
        if record["transaction_hash"] in transactions:
            native[record["transaction_hash"]].append(record)
    beneficiaries = {k.lower(): v.lower() for k, v in data.get("beneficiaries", {}).items()}
    if "native_coverage" in data:
        required = {_address(t[field]) for t in transactions.values() for field in ("from", "to")}
        required.update(beneficiaries.get(_address(t["to"]), _address(t["to"])) for t in transactions.values())
        if not required.issubset({row["address"].lower() for row in data["native_coverage"]}):
            raise ValueError("native coverage missing a participant in the accounting boundary")
    mark = Decimal(data["eth_usd_reference"])
    groups = {"by_sender": {}, "by_executor": {}}
    rows = []
    for identity, tx in transactions.items():
        receipt = receipts[identity]
        sender, executor = _address(tx["from"]), _address(tx["to"])
        if receipt["transactionHash"].lower() != identity.lower():
            raise ValueError("receipt hash mismatch")
        if _address(receipt["from"]) != sender or _address(receipt["to"]) != executor:
            raise ValueError("receipt participants mismatch")
        if int(receipt["blockNumber"], 16) != tx["block_number"]:
            raise ValueError("receipt block number mismatch")
        if (int(receipt["status"], 16) == 1) != (tx["status"] == "ok"):
            raise ValueError("receipt status mismatch")
        boundary = {sender, executor}
        if executor in beneficiaries:
            boundary.add(beneficiaries[executor])
        pre = receipt_vector(receipt, native[identity], boundary, tx.get("value", "0"))
        l2 = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
        l1 = int(receipt.get("l1Fee", "0x0"), 16)
        # Compare the sources; never add L1 to the explorer's already-total fee.
        fee = int(tx["fee"]["value"])
        if tx["fee"].get("type") != "actual" or fee != l2 + l1:
            raise ValueError("fee needs separate operator/other-component reconciliation")
        post = Counter(pre)
        post[ETH] -= fee
        unpriced = {t: str(v) for t, v in equivalent_vector(pre).items() if t not in STABLES and t != ETH_EQUIVALENT}
        row = {
            "hash": identity,
            "sender": sender,
            "executor": executor,
            "boundary": sorted(boundary),
            "success": int(receipt["status"], 16) == 1,
            "block": tx["block_number"],
            "timestamp": tx["timestamp"],
            "pre_fee_vector": {t: str(v) for t, v in equivalent_vector(pre).items()},
            "network_fee_wei": str(fee),
            "l1_fee_wei": str(l1),
            "l2_fee_wei": str(l2),
            "marked_component_after_fee_usd": str(price_component(post, mark)),
            "unpriced_components": unpriced,
            "has_negative_asset_change_before_fee": any(v < 0 for v in equivalent_vector(pre).values()),
            "standard_amm_swap_events": sum(
                log.get("topics", [None])[0] in SWAPS for log in receipt["logs"] if log.get("topics")
            ),
        }
        rows.append(row)
        for category, key in (("by_sender", sender), ("by_executor", executor)):
            group = groups[category].setdefault(
                key,
                {
                    "calls": 0,
                    "successes": 0,
                    "network_fee_wei": 0,
                    "l1_fee_wei": 0,
                    "pre_fee_vector": Counter(),
                    "marked_component_after_fee_usd": Decimal(0),
                    "calls_with_negative_asset_change": 0,
                    "calls_with_unpriced_components": 0,
                },
            )
            group["calls"] += 1
            group["successes"] += row["success"]
            group["network_fee_wei"] += fee
            group["l1_fee_wei"] += l1
            group["pre_fee_vector"].update(pre)
            group["marked_component_after_fee_usd"] += price_component(post, mark)
            group["calls_with_negative_asset_change"] += row["has_negative_asset_change_before_fee"]
            group["calls_with_unpriced_components"] += bool(unpriced)
    for category in groups.values():
        for group in category.values():
            group["pre_fee_vector"] = {t: str(v) for t, v in equivalent_vector(group["pre_fee_vector"]).items()}
            for name in ("network_fee_wei", "l1_fee_wei", "marked_component_after_fee_usd"):
                group[name] = str(group[name])
    return {
        "chain_id": 57073,
        "window": data["window"],
        "transactions": rows,
        **groups,
        "eth_usd_reference": str(mark),
        "beneficiaries": beneficiaries,
        "ownership_verified": False,
        "closed_cycle_execution_verified_for_all_rows": False,
        "scope": "Only supplied seed histories. Explicit boundaries include caller, executor and an annotated payout address if present; this attributes route cashflows, not beneficial ownership. Native-index completeness and reverted ancestors need independent checks. Marked component prices named stables at $1 and ETH/WETH at one reference, omits other assets and unrealized revaluation, and includes every scoped network fee, including failures. It is not whole-market bot P/L or a count of distinct competitors.",
    }
