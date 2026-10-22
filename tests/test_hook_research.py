import asyncio
import importlib.util
from pathlib import Path

import pytest
from eth_abi import encode
from eth_utils import keccak


spec = importlib.util.spec_from_file_location(
    "hook_research", Path(__file__).parents[1] / "scripts/inspect_xstock_hooks.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def market(monkeypatch):
    token, quote, hook = ("0x" + x * 20 for x in ("11", "22", "33"))
    definition = dict(currency0=token, currency1=quote, fee_pips=3000, tick_spacing=60, hooks=hook)
    identity = (
        "0x"
        + keccak(encode(["address", "address", "uint24", "int24", "address"], [token, quote, 3000, 60, hook])).hex()
    )
    discovery = dict(chain_id=57073, pool_manager=audit.POOL_MANAGER, complete=False, pools={identity: definition})
    assets = [dict(symbol="SPYx", deployments=[dict(network="Ink", address=token)])]

    class Rpc:
        chain = 57073
        reorg = False
        short = False

        def __init__(self, url):
            self.reads = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return self.chain

        async def sealed_block(self, block):
            assert block == 123
            self.reads += 1
            return dict(hash="0xbb" if self.reorg and self.reads > 1 else "0xaa")

        async def multicall(self, calls, block):
            assert block == 123 and len(calls) == 2
            if self.short:
                return []
            return [
                (True, encode(["uint160", "int24", "uint24", "uint24"], [1, 0, 0, 3000])),
                (True, encode(["uint128"], [100])),
            ]

        async def code_at(self, address, block):
            assert address == hook and block == 123
            return "0x6000"

    monkeypatch.setattr(audit, "ReadOnlyRpc", Rpc)
    return discovery, assets, hook, Rpc


def test_source_name_and_positive_liquidity_never_qualify_venue(market):
    discovery, assets, hook, _ = market
    explorer = {hook: dict(name="OracleHook", source_code="example", deployed_bytecode="0x6001")}
    result = asyncio.run(audit.inspect(discovery, assets, explorer, "unused", 123))
    assert result["summary"]["positive_liquidity_hooks"] == 1
    assert result["summary"]["qualified_oracle_venues"] == 0
    assert not result["hooks"][hook]["explorer_runtime_matches"]
    assert not result["discovery_complete_at_checkpoint"]
    assert result["hooks"][hook]["source_hash"] == "0x" + keccak(text="example").hex()


def test_archived_issuer_pages_and_unresolved_hook_remain_explicit(market):
    discovery, assets, hook, _ = market
    catalog = dict(complete=True, pages=[dict(nodes=assets)])
    result = asyncio.run(audit.inspect(discovery, catalog, {}, "unused", 123))
    assert result["issuer_catalog_complete"] and result["issuer_asset_count"] == 1
    assert result["summary"]["unresolved_active_hooks"] == 1
    assert result["hooks"][hook]["explorer_name"] is None
    assert not result["hooks"][hook]["explorer_runtime_matches"]


@pytest.mark.parametrize("failure", ["chain", "reorg", "short", "pool_key"])
def test_invalid_evidence_does_not_produce_report(market, failure):
    discovery, assets, _, rpc = market
    if failure == "chain":
        rpc.chain = 1
    elif failure == "reorg":
        rpc.reorg = True
    elif failure == "short":
        rpc.short = True
    else:
        next(iter(discovery["pools"].values()))["tick_spacing"] = 10
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(audit.inspect(discovery, assets, {}, "unused", 123))
