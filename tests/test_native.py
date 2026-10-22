import asyncio
import json

from eth_abi import encode
from eth_utils import keccak
import pytest

from incy_arb import flash_credit, v4_state
from incy_arb.fees import assess_net_profit
from incy_arb.flash_credit import NATIVE_CURRENCY as NATIVE, POOL_MANAGER
from incy_arb.research import _eligible_v4_pools, _flash_balances, ink_compatibility, xarb2_compatibility
from incy_arb.simulation import encode_simulation_route

A = "0x" + "11" * 20
B = "0x" + "22" * 20
ZERO = NATIVE


def step(token_in, token_out, fee):
    a, b = sorted((token_in, token_out))
    key = (a, b, fee, 60, ZERO)
    identity = "0x" + keccak(encode(["(address,address,uint24,int24,address)"], [key])).hex()
    return {
        "venue": "uniswap_v4",
        "token_in": token_in,
        "token_out": token_out,
        "pool": {"address": identity, "token0": a, "token1": b, "fee_pips": fee, "tick_spacing": 60, "hooks": ZERO},
    }


def test_native_start_and_payout_are_encoded_without_erc20_transfer_steps():
    route = {"start_token": NATIVE, "amount_in": "1000", "steps": [step(NATIVE, A, 3000), step(A, NATIVE, 500)]}
    data = encode_simulation_route(route, 123)
    assert data[4:24] == bytes(20) and len(data) == 60 + 47 + 47
    assert data[108:128] == bytes(20)  # second v4 output currency


def test_native_intermediate_can_connect_v4_to_external_erc20_steps():
    route = {
        "start_token": A,
        "amount_in": 1000,
        "steps": [
            step(A, NATIVE, 3000),
            step(NATIVE, B, 500),
            {
                "venue": "uniswap_v2",
                "token_in": B,
                "token_out": A,
                "pool": {"address": "0x" + "33" * 20, "token0": A, "token1": B, "fee_bps": 30},
            },
        ],
    }
    assert encode_simulation_route(route, 123)[154] == 4
    route["steps"][1]["venue"] = "slipstream"
    with pytest.raises(ValueError, match="native"):
        encode_simulation_route(route, 123)


def test_old_erc20_only_state_capture_can_be_reclassified_without_losing_scope_control():
    pool = step(NATIVE, A, 3000)["pool"]
    identity = pool["address"]
    discovery = {
        "pools": {identity: {"currency0": NATIVE, "currency1": A, "fee_pips": 3000, "tick_spacing": 60, "hooks": ZERO}}
    }
    state = {"states": {identity: {"readable": True, "liquidity": "100", "eligible_for_future_quote": False}}}
    assert len(_eligible_v4_pools(discovery, state)) == 1
    assert _eligible_v4_pools(discovery, state, include_native=False) == []
    discovery["pools"][identity]["hooks"] = A
    assert _eligible_v4_pools(discovery, state) == []


def test_native_compatibility_is_distinct_from_upstream_xarb2():
    path = [("uniswap_v4", {}, NATIVE, A), ("uniswap_v4", {}, A, NATIVE)]
    assert ink_compatibility(path) == (True, [])
    assert not xarb2_compatibility(path)[0]
    assert not ink_compatibility([("slipstream", {}, NATIVE, A)])[0]


class NativeRpc:
    def __init__(self, *args, **kwargs):
        self.native_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def chain_id(self):
        return 57073

    async def block_number(self):
        return 123

    async def sealed_block(self, block):
        return {"hash": "0xaa", "timestamp": "0x1"}

    async def native_balance(self, address, block):
        assert address == POOL_MANAGER and block == 123
        self.native_reads += 1
        return 12345

    async def multicall(self, calls, block):
        assert block == 123
        assert all(call.target != NATIVE for call in calls)
        return [(True, encode(["uint256"], [400])) for call in calls]


def test_flash_capacity_reads_eth_balance_instead_of_balanceof_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(flash_credit, "ReadOnlyRpc", NativeRpc)
    result = asyncio.run(
        flash_credit.snapshot_flash_capacity(flash_credit.FlashCapacityConfig((NATIVE,), tmp_path / "native.json"))
    )
    value = result["balances"][0]
    assert value["currency_kind"] == "native" and value["symbol"] == "ETH" and value["decimals"] == 18
    assert value["pool_manager_balance"] == "12345"
    balances = asyncio.run(_flash_balances(NativeRpc(), 123, (NATIVE, A)))
    assert balances == {NATIVE: 12345, A: 400}


def test_native_stateview_pool_is_eligible_without_being_an_erc20_pair(tmp_path, monkeypatch):
    definition = step(NATIVE, A, 3000)["pool"]
    identity = definition["address"]
    path = tmp_path / "discovery.json"
    path.write_text(
        json.dumps(
            {
                "chain_id": 57073,
                "pool_manager": POOL_MANAGER,
                "complete": True,
                "pools": {
                    identity: {"currency0": NATIVE, "currency1": A, "fee_pips": 3000, "tick_spacing": 60, "hooks": ZERO}
                },
            }
        )
    )

    class StateRpc(NativeRpc):
        async def multicall(self, calls, block):
            return [
                (True, encode(["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, 0, 3000])),
                (True, encode(["uint128"], [1000000])),
            ]

    monkeypatch.setattr(v4_state, "ReadOnlyRpc", StateRpc)
    result = asyncio.run(v4_state.snapshot_v4_state(v4_state.V4StateConfig(path, tmp_path / "state.json")))
    assert result["states"][identity]["eligible_for_future_quote"]
    assert result["states"][identity]["native_currency"] and not result["states"][identity]["erc20_currencies"]


def test_native_profit_and_eth_fee_budget_have_the_same_units():
    simulation = {
        "status": "success",
        "gas_estimate": 1000,
        "block_hash": "0xaa",
        "block_number": 123,
        "start_token": NATIVE,
        "simulated_gross_pnl": "10000",
    }
    budget = {"status": "estimated", "block_hash": "0xaa", "block_number": 123, "total_fee_budget_wei": "5000"}
    assert assess_net_profit(simulation, budget)["estimated_net_pnl_native_wei"] == "5000"
