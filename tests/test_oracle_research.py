import asyncio
import json

import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from incy_arb import oracle_research as oracle
from incy_arb.rpc import RpcError

BASE = "0x" + "11" * 20
QUOTE = "0x" + "22" * 20
PAIR = keccak(bytes.fromhex(BASE[2:] + QUOTE[2:]))
UNKNOWN = b"\xff" * 32


@pytest.fixture
def probe(tmp_path, monkeypatch):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"chain_id": 57073, "tokens": {BASE: {}, QUOTE: {}}}))

    class Rpc:
        chain = 57073
        reorg = False
        ids = [PAIR]
        mode = "revert"
        code = "0x6000"
        pages = []

        def __init__(self, url):
            self.reads = 0
            type(self).pages = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return self.chain

        async def block_number(self):
            return 123

        async def sealed_block(self, block):
            assert block == 123
            self.reads += 1
            return {"hash": "0xbb" if self.reorg and self.reads > 1 else "0xaa"}

        async def code_at(self, venue, block):
            assert venue == oracle.KALIBER_INK and block == 123
            return self.code

        async def contract_call(self, venue, data, block):
            assert venue == oracle.KALIBER_INK and block == 123
            raw = data
            if raw[:4] == keccak(text="getAllPairIds(uint256,uint256)")[:4]:
                start, count = decode(["uint256", "uint256"], raw[4:])
                self.pages.append((start, count))
                ids = self.ids if self.mode == "repeat" else self.ids[start : start + count]
                return "0x" + encode(["bytes32[]"], [ids]).hex()
            if raw[:4] == keccak(text="getPoolBalances(bytes32)")[:4]:
                return "0x" + encode(["uint256", "uint256"], [100000, 10000]).hex()
            assert raw[:4] == keccak(text="quote(bytes32,address,address,uint256)")[:4]
            identity, token_in, token_out, amount = decode(["bytes32", "address", "address", "uint256"], raw[4:])
            assert identity == PAIR and {token_in, token_out} == {BASE, QUOTE} and amount > 0
            if self.mode == "revert":
                raise RpcError("execution reverted", code=3, data={"data": "0x2af96ae8"})
            if self.mode == "rpc_error":
                raise RpcError("provider unavailable", code=-32000)
            return "0x" + encode(["uint256"], [0 if self.mode == "zero" else amount * 2]).hex()

    monkeypatch.setattr(oracle, "ReadOnlyRpc", Rpc)
    return oracle.OracleSnapshotConfig(path, page_size=1, max_pages=4), Rpc


def test_pagination_and_reverts_are_preserved_without_inventing_prices(probe):
    config, rpc = probe
    result = asyncio.run(oracle.snapshot_oracle_venue(config))
    assert rpc.pages == [(0, 1), (1, 1)] and result["enumeration_complete"]
    assert result["summary"]["quote_attempts"] == result["summary"]["reverted_quotes"] == 4
    assert result["summary"]["positive_quotes"] == 0
    assert result["strategy_status"] == "unconfirmed"
    for attempt in result["pools"][0]["quotes"]:
        assert attempt["error_selector"] == "0x2af96ae8" and "amount_out" not in attempt
    assert result["runtime_code_hash"] == "0x" + keccak(bytes.fromhex(rpc.code[2:])).hex()


@pytest.mark.parametrize(
    "mode,status", [("positive", "positive_quote"), ("zero", "zero_quote"), ("rpc_error", "rpc_error")]
)
def test_quote_status_does_not_certify_execution(probe, mode, status):
    config, rpc = probe
    rpc.mode = mode
    result = asyncio.run(oracle.snapshot_oracle_venue(config))
    pair = result["pools"][0]
    assert {q["status"] for q in pair["quotes"]} == {status}
    assert not pair["execution_verified"] and result["strategy_status"] == "unconfirmed"


def test_unknown_pair_is_reported_without_guessing_tokens(probe):
    config, rpc = probe
    rpc.ids = [UNKNOWN]
    result = asyncio.run(oracle.snapshot_oracle_venue(config))
    assert result["summary"]["registered_pairs"] == 1 and result["summary"]["resolved_pairs"] == 0
    assert result["pools"][0]["quotes"] == [] and not result["pools"][0]["tokens_resolved"]
    assert oracle.resolve_pair_ids([PAIR], {BASE: {}, "not-an-address": {}}) == {}
    assert oracle.resolve_pair_ids([PAIR], {BASE: {}, QUOTE: {}}) == {PAIR: (BASE, QUOTE)}


def test_page_cap_is_explicit_and_repeated_ids_fail(probe):
    config, rpc = probe
    capped = oracle.OracleSnapshotConfig(config.inventory, page_size=1, max_pages=1)
    assert not asyncio.run(oracle.snapshot_oracle_venue(capped))["enumeration_complete"]
    rpc.mode = "repeat"
    with pytest.raises(ValueError, match="pagination"):
        asyncio.run(oracle.snapshot_oracle_venue(config))


@pytest.mark.parametrize("failure", ["chain", "reorg", "no_code", "inventory_chain"])
def test_wrong_chain_missing_code_and_reorg_fail_closed(probe, failure):
    config, rpc = probe
    if failure == "chain":
        rpc.chain = 1
    elif failure == "reorg":
        rpc.reorg = True
    elif failure == "no_code":
        rpc.code = "0x"
    else:
        config.inventory.write_text(json.dumps({"chain_id": 1}))
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(oracle.snapshot_oracle_venue(config))
