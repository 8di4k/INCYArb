import argparse
import asyncio
import json
from dataclasses import replace

import pytest

from incy_arb import research
from incy_arb.__main__ import _size_range
from incy_arb.quotes import constant_product_out
from incy_arb.rpc import RpcError
from incy_arb.sizing import optimize_size, optimize_size_async


def test_async_search_matches_sync_and_never_repeats_quotes():
    calls = []

    def curve(amount):
        return None if amount > 90_000 else max(0, amount + 2_000 - abs(amount - 50_000) // 10)

    async def quote(amount):
        await asyncio.sleep(0)
        calls.append(amount)
        return curve(amount)

    result = asyncio.run(optimize_size_async(quote, 100, 100_000, 100_000))
    assert result == optimize_size(curve, 100, 100_000, 100_000)
    assert len(calls) == len(set(calls))


@pytest.fixture
def scan_setup(tmp_path, monkeypatch):
    pools = [dict(address=p, token0='a', token1='b', has_nonzero_reserves=True,
                  recently_updated=True, fee_bps=30) for p in ('p1', 'p2')]
    files = []
    for name, data in [('inventory', {'uniswap_v2': pools}), ('discovery', {'pools': {}}), ('state', {'states': {}})]:
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps({'chain_id': 57073, **data}))
        files.append(path)

    class Rpc:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return 57073

        async def block_number(self):
            return 123

        async def sealed_block(self, block):
            assert block == 123
            return {'timestamp': '0x100', 'hash': '0xaa'}

    async def current(rpc, block, pools):
        assert block == 123
        return {'p1': {'reserve0': 100_000, 'reserve1': 200_000, 'fee_bps': 30},
                'p2': {'reserve0': 100_000, 'reserve1': 100_000, 'fee_bps': 30}}

    async def balances(rpc, block, tokens):
        assert block == 123
        return {'a': 10_000}

    monkeypatch.setattr(research, 'ReadOnlyRpc', Rpc)
    monkeypatch.setattr(research, '_current_v2', current)
    monkeypatch.setattr(research, '_flash_balances', balances)
    return research.CycleScanConfig(files[0], (), v4_discovery=files[1], v4_state=files[2],
                                    starts=('a',), size_ranges=(('a', 100, 30_000),), max_steps=2)


def test_cycle_sizing_matches_exhaustive_v2_profit_and_clamps_balance(scan_setup):
    result = asyncio.run(research.scan_cycles(scan_setup))
    assert result['quote_block_number'] == 123
    assert result['structural_cycles'] == 2
    assert len(result['findings']) == 1
    best = result['findings'][0]
    assert best['pools'] == ['p1', 'p2']
    profits = [constant_product_out(constant_product_out(x, 100_000, 200_000, 30),
                                   100_000, 100_000, 30) - x for x in range(100, 10_001)]
    assert int(best['gross_pnl']) >= max(profits) - 2
    assert int(best['amount_in']) <= 10_000
    assert best['sizing']['effective_max'] == '10000'
    assert best['flash_capacity_sufficient']
    assert {entry['status'] for entry in result['sizing']} == {'positive_gross_quote', 'no_positive_gross_quote'}


def test_fixed_amount_scan_still_reports_loss_and_insufficient_capacity(scan_setup):
    config = replace(scan_setup, size_ranges=(), amounts=(20_000,))
    result = asyncio.run(research.scan_cycles(config))
    assert len(result['findings']) == 2
    assert result['sizing'] == []
    assert all(row['amount_in'] == '20000' and not row['flash_capacity_sufficient'] for row in result['findings'])
    assert any(not row['profitable_before_gas'] for row in result['findings'])


def test_v4_first_can_size_with_zero_start_balance_but_external_first_cannot(scan_setup, monkeypatch):
    path = [('uniswap_v4', {'address': 'v4'}, 'a', 'b'),
            ('slipstream', {'address': 'cl'}, 'b', 'a')]
    external_path = [('slipstream', {'address': 'cl'}, 'a', 'b'),
                     ('uniswap_v4', {'address': 'v4'}, 'b', 'a')]
    monkeypatch.setattr(research, 'cycle_paths', lambda *args: [path, external_path])
    calls = []

    async def balances(*args):
        return {'a': 0}

    async def quote(rpc, block, route, amount, current):
        assert block == 123
        assert route[0][0] == 'uniswap_v4'
        calls.append(amount)
        return amount + 100, 42

    monkeypatch.setattr(research, '_flash_balances', balances)
    monkeypatch.setattr(research, '_quote_cycle', quote)
    result = asyncio.run(research.scan_cycles(scan_setup))
    assert len(result['findings']) == 1
    assert result['findings'][0]['initial_take_required'] is False
    assert result['findings'][0]['amount_in'] == '30000'
    assert result['findings'][0]['quoter_gas_estimate'] == 42
    assert len(calls) == len(set(calls))
    assert result['sizing'][1]['status'] == 'insufficient_initial_capacity'
    assert result['sizing'][1]['evaluations'] == 0


def test_failed_quoter_is_visible_without_becoming_profit(scan_setup, monkeypatch):
    path = [('slipstream', {'address': 'cl', 'tick_spacing': 100}, 'a', 'b'),
            ('slipstream', {'address': 'cl2', 'tick_spacing': 100}, 'b', 'a')]
    monkeypatch.setattr(research, 'cycle_paths', lambda *args: [path])

    async def fail(*args):
        raise RpcError('quoter reverted')

    monkeypatch.setattr(research, '_quote', fail)
    result = asyncio.run(research.scan_cycles(scan_setup))
    assert result['findings'] == []
    metadata = result['sizing'][0]
    assert metadata['status'] == 'all_quotes_failed'
    assert metadata['failed_quotes'] == metadata['evaluations'] > 0


@pytest.mark.parametrize('ranges', [(('a', 0, 1),), (('a', 20, 10),), (('b', 1, 10),), (('a', 1, 10), ('a', 1, 10))])
def test_invalid_sizing_ranges_fail_before_rpc(scan_setup, ranges):
    with pytest.raises(ValueError):
        asyncio.run(research.scan_cycles(replace(scan_setup, size_ranges=ranges)))


def test_size_range_uses_token_specific_raw_units():
    assert _size_range('0xAbC=100:5000') == ('0xabc', 100, 5000)
    for value in ('a=0:1', 'a=10:1', 'a=foo:2', 'a=1', '=1:2'):
        with pytest.raises(argparse.ArgumentTypeError):
            _size_range(value)


def test_missing_balance_does_not_claim_observed_zero_balance(scan_setup, monkeypatch):
    async def balances(*args):
        return {}

    monkeypatch.setattr(research, '_flash_balances', balances)
    result = asyncio.run(research.scan_cycles(scan_setup))
    assert result['findings'] == []
    assert all(row['status'] == 'flash_balance_unavailable' and row['evaluations'] == 0
               for row in result['sizing'])


def test_scan_rejects_wrong_rpc_chain_before_quoting(scan_setup, monkeypatch):
    parent = research.ReadOnlyRpc
    class WrongChain(parent):
        async def chain_id(self): return 196
    monkeypatch.setattr(research,'ReadOnlyRpc',WrongChain)
    with pytest.raises(ValueError,match='chain ID'):
        asyncio.run(research.scan_cycles(scan_setup))


def test_scan_discards_findings_if_its_pinned_block_is_replaced(scan_setup, monkeypatch):
    parent = research.ReadOnlyRpc
    class Reorg(parent):
        def __init__(self,url): self.reads=0
        async def sealed_block(self,block):
            self.reads += 1
            return {'hash': '0xaa' if self.reads == 1 else '0xbb','timestamp': '0x100'}
    monkeypatch.setattr(research,'ReadOnlyRpc',Reorg)
    with pytest.raises(RuntimeError,match='discard findings'):
        asyncio.run(research.scan_cycles(scan_setup))
