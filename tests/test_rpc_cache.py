import asyncio

import pytest

from incy_arb.rpc import PinnedQuoteCache, ReadOnlyRpc, RpcError

TARGET = '0x' + 'ab' * 20


@pytest.mark.parametrize('code', [3, -32601, -32602])
def test_deterministic_rpc_error_is_not_retried(code):
    rpc = ReadOnlyRpc('https://example.test')
    calls = []
    async def request(*args):
        calls.append(args)
        raise RpcError('deterministic', code=code, data='0xdeadbeef')
    rpc._request = request
    with pytest.raises(RpcError) as error:
        asyncio.run(rpc.contract_call(TARGET,b'abc',123))
    assert error.value.code == code and len(calls) == 1


def test_transient_rpc_failure_still_retries(monkeypatch):
    rpc = ReadOnlyRpc('https://example.test')
    calls, delays = [], []
    async def request(*args):
        calls.append(args)
        if len(calls) == 1: raise RpcError('HTTP 503')
        return '0x1234'
    async def sleep(delay): delays.append(delay)
    rpc._request = request
    monkeypatch.setattr(asyncio,'sleep',sleep)
    assert asyncio.run(rpc.contract_call(TARGET,b'abc',123)) == '0x1234'
    assert len(calls) == 2 and delays == [0.3]


class ScriptedCaller:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    async def contract_call(self, target, data, block, attempts=4):
        self.calls.append((target,data,block))
        value = next(self.responses)
        if isinstance(value,Exception): raise value
        return value


def test_same_pinned_call_reuses_output_across_paths():
    rpc = ScriptedCaller(['0x1234'])
    cache = PinnedQuoteCache(rpc,123)
    async def run():
        assert await cache.contract_call(TARGET,b'abc',123) == '0x1234'
        assert await cache.contract_call('0x'+TARGET[2:].upper(),b'abc',123) == '0x1234'
    asyncio.run(run())
    assert len(rpc.calls) == 1 and cache.summary()['hits'] == 1
    for block in (124,'pending','latest','0x7b'):
        with pytest.raises(ValueError): asyncio.run(cache.contract_call(TARGET,b'abc',block))


def test_pinned_execution_revert_is_cached_but_transport_failure_is_not():
    rpc = ScriptedCaller([RpcError('reverted',code=3,data='0xdeadbeef')])
    cache = PinnedQuoteCache(rpc,123)
    for _ in range(2):
        with pytest.raises(RpcError) as error: asyncio.run(cache.contract_call(TARGET,b'abc',123))
        assert error.value.data == '0xdeadbeef'
    assert len(rpc.calls) == 1
    rpc = ScriptedCaller([RpcError('transport failure'),'0xbeef'])
    cache = PinnedQuoteCache(rpc,123)
    with pytest.raises(RpcError): asyncio.run(cache.contract_call(TARGET,b'abc',123))
    assert asyncio.run(cache.contract_call(TARGET,b'abc',123)) == '0xbeef'
    assert len(rpc.calls) == 2


def test_quote_cache_does_not_grow_beyond_its_bound():
    rpc = ScriptedCaller(['0x01','0x02','0x02'])
    cache = PinnedQuoteCache(rpc,123,max_entries=1)
    async def run():
        assert await cache.contract_call(TARGET,b'a',123) == '0x01'
        for _ in range(2): assert await cache.contract_call(TARGET,b'b',123) == '0x02'
        assert await cache.contract_call(TARGET,b'a',123) == '0x01'
    asyncio.run(run())
    assert len(cache.entries) == 1 and len(rpc.calls) == 3
