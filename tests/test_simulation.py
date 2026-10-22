import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest
from eth_abi import encode
from eth_utils import keccak

from incy_arb.abi import function_selector
from incy_arb.flash_credit import INT128_MAX, POOL_MANAGER
from incy_arb.rpc import RpcError
from incy_arb.simulation import (
    SIM_ADDRESS, SIM_CALLER, decode_simulation_error, encode_simulation_route, simulate_finding,
)

A = '0x' + '11' * 20
B = '0x' + '22' * 20
P1 = '0x' + '33' * 20
P2 = '0x' + '44' * 20


def route():
    return {'start_token': A, 'amount_in': '1000', 'steps': [
        {'venue': 'uniswap_v2', 'pool': {'address': P1, 'token0': A, 'token1': B, 'fee_bps': 30},
         'token_in': A, 'token_out': B},
        {'venue': 'slipstream', 'pool': {'address': P2, 'token0': A, 'token1': B},
         'token_in': B, 'token_out': A},
    ]}


def scan():
    return {'chain_id': 57073, 'quote_block_number': 123,
            'findings': [{'simulation_route': route(), 'gross_pnl': '300'}]}


def test_packed_route_preserves_header_and_step_boundaries():
    data = encode_simulation_route(route(), 123)
    assert data[:4] == function_selector('execute()')
    assert data[4:24] == bytes.fromhex(A[2:])
    assert int.from_bytes(data[24:40], 'big') == 1000
    assert int.from_bytes(data[56:60], 'big') == 123
    assert data[60] == 4 and data[103] == 1
    assert len(data) == 60 + 43 + 41


def test_v4_requires_matching_key_and_zero_hooks():
    value = route()
    key = (A, B, 3000, 60, '0x' + '00' * 20)
    pool_id = '0x' + keccak(encode(['(address,address,uint24,int24,address)'], [key])).hex()
    value['steps'][0] = {'venue': 'uniswap_v4', 'token_in': A, 'token_out': B,
                         'pool': {'address': pool_id, 'token0': A, 'token1': B,
                                  'fee_pips': 3000, 'tick_spacing': 60, 'hooks': key[-1]}}
    assert len(encode_simulation_route(value, 123)) == 60 + 47 + 41
    value['steps'][0]['pool']['address'] = '0x' + 'ff' * 32
    with pytest.raises(ValueError, match='pool ID'):
        encode_simulation_route(value, 123)
    value['steps'][0]['pool']['address'] = pool_id
    value['steps'][0]['pool']['hooks'] = P1
    with pytest.raises(ValueError, match='zero hooks'):
        encode_simulation_route(value, 123)


@pytest.mark.parametrize('change', ['stable', 'unknown', 'continuity', 'pool_sides', 'not_cycle', 'over_limit', 'repeated'])
def test_unsupported_or_malformed_routes_fail_before_rpc(change):
    value = route()
    if change == 'stable': value['steps'][0]['pool']['stable'] = True
    if change == 'unknown': value['steps'][0]['venue'] = 'adapter'
    if change == 'continuity': value['steps'][1]['token_in'] = A
    if change == 'pool_sides': value['steps'][0]['pool']['token1'] = P1
    if change == 'not_cycle': value['steps'].pop()
    if change == 'over_limit': value['amount_in'] = INT128_MAX + 1
    if change == 'repeated': value['steps'][1]['pool']['address'] = P1
    with pytest.raises(ValueError):
        encode_simulation_route(value, 123)


class FakeRpc:
    def __init__(self):
        self.outcome = '0x' + encode(['uint256'], [305]).hex()
        self.reorg = False
        self.block_reads = 0
        self.getter_ok = True
        self.gas_error = False
        self.calls = []

    async def chain_id(self): return 57073
    async def sealed_block(self, block):
        assert block == 123
        self.block_reads += 1
        return {'hash': '0xbb' if self.reorg and self.block_reads > 1 else '0xaa'}
    async def code_at(self, address, block): return '0x'
    async def simulation_call(self, tx, block, overrides):
        assert block == 123
        self.calls.append((tx, copy.deepcopy(overrides)))
        assert set(overrides) <= {SIM_ADDRESS, SIM_CALLER}
        assert 'stateDiff' not in overrides[SIM_CALLER]
        if 'to' not in tx: return '0x' + 'ab' * 100
        data = tx['data'][2:10]
        if data == function_selector('poolManager()').hex():
            return hex(int(POOL_MANAGER, 16) if self.getter_ok else 1)
        if data == function_selector('isExecutor(address)').hex(): return hex(1)
        if data == function_selector('beneficiary()').hex(): return hex(int(SIM_CALLER,16))
        if isinstance(self.outcome, Exception): raise self.outcome
        return self.outcome
    async def simulation_gas(self, tx, block, overrides):
        assert block == 123
        if self.gas_error: raise RpcError('unsupported overrides', code=-32602)
        return 210_000


def test_success_reports_full_transaction_gas_and_quote_difference():
    rpc = FakeRpc()
    result = asyncio.run(simulate_finding(rpc, scan()))
    assert result['status'] == 'success'
    assert result['simulated_gross_pnl'] == '305' and result['quote_difference'] == '5'
    assert result['gas_estimate'] == 210_000
    assert result['market_state_overridden'] is False
    assert rpc.block_reads == 2


def test_reverting_loss_preserves_signed_delta_and_does_not_estimate_success_gas():
    rpc = FakeRpc()
    data = '0x' + (function_selector('NegativeProfit(int256)') + encode(['int256'], [-511])).hex()
    rpc.outcome = RpcError('execution reverted', code=3, data=data)
    value = scan(); value['findings'][0]['gross_pnl'] = '-511'
    result = asyncio.run(simulate_finding(rpc, value))
    assert result['status'] == 'execution_reverted'
    assert result['simulated_gross_pnl'] == '-511' and result['quote_difference'] == '0'
    assert 'gas_estimate' not in result


def test_rpc_failure_is_not_labeled_execution_revert_or_success():
    rpc = FakeRpc(); rpc.outcome = RpcError('transport failed')
    result = asyncio.run(simulate_finding(rpc, scan()))
    assert result['status'] == 'rpc_error'
    assert 'simulated_gross_pnl' not in result


def test_gas_failure_keeps_fee_evaluation_unavailable():
    rpc = FakeRpc(); rpc.gas_error = True
    result = asyncio.run(simulate_finding(rpc, scan()))
    assert result['gas_estimate'] is None and 'gas_error' in result


@pytest.mark.parametrize('failure', ['reorg', 'getter'])
def test_reorg_and_wrong_injected_storage_fail_closed(failure):
    rpc = FakeRpc()
    if failure == 'reorg': rpc.reorg = True
    else: rpc.getter_ok = False
    with pytest.raises(RuntimeError):
        asyncio.run(simulate_finding(rpc, scan()))


def test_error_decoder_keeps_unknown_and_malformed_data():
    unknown = RpcError('reverted', code=3, data='0xdeadbeef')
    assert decode_simulation_error(unknown)['revert_data'] == '0xdeadbeef'
    malformed = RpcError('reverted', code=3, data='0x' + function_selector('NegativeProfit(int256)').hex())
    assert decode_simulation_error(malformed)['malformed_error_data']


def test_committed_artifact_matches_solidity_sources():
    root = Path(__file__).resolve().parents[1]
    artifact = json.loads((root/'src/incy_arb/artifacts/InkArb.json').read_text())
    for name, expected in artifact['source_sha256'].items():
        assert hashlib.sha256((root/'contracts'/name).read_bytes()).hexdigest() == expected
    assert artifact['compiler'] == '0.8.28+commit.7893614a'
    assert artifact['storage_layout']['isExecutor'] == {'slot': '2', 'offset': 0}
