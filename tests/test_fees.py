import asyncio

import pytest
import rlp
from eth_abi import decode, encode

from incy_arb.abi import function_selector
from incy_arb.fees import WETH, assess_net_profit, fee_budget, unsigned_envelope
from incy_arb.rpc import RpcError

TARGET = '0x' + '11' * 20


@pytest.mark.parametrize('length', [0, 140, 1024])
def test_unsigned_envelope_matches_type2_fields_and_oracle_signature_allowance(length):
    calldata = b'\xaa' * length
    envelope = unsigned_envelope(calldata, TARGET, 200000, 100, 2, 7)
    decoded = rlp.decode(envelope[1:])
    assert envelope[0] == 2
    assert int.from_bytes(decoded[0], 'big') == 57073
    assert int.from_bytes(decoded[1], 'big') == 7
    assert int.from_bytes(decoded[4], 'big') == 200000
    assert decoded[5] == bytes.fromhex(TARGET[2:]) and decoded[6] == b''
    assert decoded[7] == calldata and decoded[8] == []
    signed = b'\x02' + rlp.encode(decoded + [b'\x01', b'\xff' * 32, b'\x7f' + b'\xff' * 31])
    assert len(signed) <= len(envelope) + 68


class FeeRpc:
    def __init__(self):
        self.calls = []
        self.reads = 0
        self.reorg = False
        self.unavailable = False
        self.content_unavailable = False
    async def chain_id(self): return 57073
    async def sealed_block(self, block):
        assert block == 123
        self.reads += 1
        return {'hash': '0xbb' if self.reorg and self.reads > 1 else '0xaa', 'baseFeePerGas': '0x5'}
    async def contract_call(self, target, data, block):
        assert block == 123
        if data[:4] == function_selector('getL1Fee(bytes)'):
            envelope = decode(['bytes'], data[4:])[0]
            self.calls.append((data[:4], envelope))
            if self.content_unavailable: raise RpcError('content estimation unavailable', code=-32601)
            return '0x' + encode(['uint256'], [1000]).hex()
        self.calls.append((data[:4], decode(['uint256'], data[4:])[0]))
        if self.unavailable: raise RpcError('unsupported method', code=3)
        value = 2000 if data[:4] == function_selector('getL1FeeUpperBound(uint256)') else 300
        return '0x' + encode(['uint256'], [value]).hex()


def test_budget_sums_l2_l1_and_operator_at_one_block_without_double_signature_padding():
    rpc = FeeRpc()
    result = asyncio.run(fee_budget(rpc, b'\xaa' * 140, TARGET, 123, 200000, priority_fee=2))
    assert result['max_fee_per_gas'] == '12'
    assert result['l2_fee_at_maximum'] == '2400000'
    assert result['total_fee_budget_wei'] == '2402300'
    assert rpc.calls == [(function_selector('getL1FeeUpperBound(uint256)'), result['unsigned_transaction_size']),
                         (function_selector('getOperatorFee(uint256)'), 200000),
                         (function_selector('getL1Fee(bytes)'), unsigned_envelope(b'\xaa' * 140, TARGET, 200000, 12, 2))]
    assert result['content_based_estimate']['total_fee_estimate_wei'] == '2401300'
    assert result['content_based_estimate']['l1_fee_estimate'] == '1000'
    assert bytes.fromhex(result['content_based_estimate']['unsigned_transaction'][2:]) == rpc.calls[-1][1]


def test_oracle_failure_does_not_default_l1_or_operator_to_zero():
    rpc = FeeRpc(); rpc.unavailable = True
    with pytest.raises(RpcError):
        asyncio.run(fee_budget(rpc, b'', TARGET, 123, 200000))


def test_budget_discards_reorg():
    rpc = FeeRpc(); rpc.reorg = True
    with pytest.raises(RuntimeError):
        asyncio.run(fee_budget(rpc, b'', TARGET, 123, 200000))


def simulation(token=WETH, profit='10000'):
    return {'status': 'success', 'gas_estimate': 200000, 'block_number': 123, 'block_hash': '0xaa',
            'start_token': token, 'simulated_gross_pnl': profit}


def budget():
    return {'status': 'estimated', 'block_number': 123, 'block_hash': '0xaa', 'total_fee_budget_wei': '5000'}


def test_weth_profit_uses_native_wei_and_deducts_complete_budget():
    result = assess_net_profit(simulation(), budget())
    assert result['status'] == 'estimated_positive' and result['estimated_net_pnl_native_wei'] == '5000'
    assert assess_net_profit(simulation(profit='4000'), budget())['status'] == 'not_positive_after_fee_budget'


def test_stablecoin_raw_units_cannot_be_subtracted_from_eth_wei():
    result = assess_net_profit(simulation(TARGET, '1000000000000000000000000'), budget())
    assert result['status'] == 'start_token_valuation_required'
    assert 'estimated_net_pnl_native_wei' not in result


def test_unconfirmed_execution_and_missing_fee_estimate_never_become_profit():
    value = simulation(); value['status'] = 'execution_reverted'
    assert assess_net_profit(value, budget())['status'] == 'execution_not_confirmed'
    assert assess_net_profit(simulation(), {'status': 'unavailable'})['status'] == 'fee_budget_unavailable'


def test_fee_budget_cannot_be_applied_to_another_block():
    value = budget(); value['block_hash'] = '0xbb'
    with pytest.raises(ValueError): assess_net_profit(simulation(), value)


@pytest.mark.parametrize('gas,maximum,priority,nonce', [(0,1,0,0), (1,1,2,0), (1,1,-1,0), (1,1,0,-1)])
def test_invalid_transaction_envelope_values_are_rejected(gas,maximum,priority,nonce):
    with pytest.raises(ValueError): unsigned_envelope(b'', TARGET, gas, maximum, priority, nonce)



def test_content_estimation_failure_preserves_successful_upper_budget():
    rpc = FeeRpc(); rpc.content_unavailable = True
    result = asyncio.run(fee_budget(rpc, b'\xaa' * 140, TARGET, 123, 200000))
    assert result['status'] == 'estimated' and result['total_fee_budget_wei'] == '2002300'
    assert result['content_based_estimate']['status'] == 'unavailable'
    assert 'total_fee_estimate_wei' not in result['content_based_estimate']


def test_positive_compression_estimate_never_promotes_negative_upper_budget():
    value = budget()
    value['content_based_estimate'] = {'status': 'estimated', 'total_fee_estimate_wei': '3000'}
    result = assess_net_profit(simulation(profit='4000'), value)
    assert result['status'] == 'not_positive_after_fee_budget'
    assert result['estimated_net_pnl_native_wei'] == '-1000'
    assert result['content_based_estimate']['status'] == 'estimated_positive'
    assert result['content_based_estimate']['estimated_net_pnl_native_wei'] == '1000'


def test_compression_estimate_does_not_convert_stablecoin_pnl_to_eth():
    value = budget()
    value['content_based_estimate'] = {'status': 'estimated', 'total_fee_estimate_wei': '3000'}
    result = assess_net_profit(simulation(TARGET, '100000'), value)
    assert result['status'] == 'start_token_valuation_required' and 'content_based_estimate' not in result
