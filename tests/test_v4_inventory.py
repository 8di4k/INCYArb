import asyncio
import json

import pytest

from incy_arb import v4_inventory
from incy_arb.v4_inventory import parse_initialize


def test_parse_initialize_retains_canonical_source_location() -> None:
    event = {
        "block_number": 42,
        "block_hash": "0xblock",
        "index": 7,
        "transaction_hash": "0xtx",
        "block_timestamp": "2026-10-22T16:20:44.000000Z",
        "decoded": {
            "abi": {"name": "Initialize"},
            "parameters": [
                {"name": "id", "value": "0xABCD"},
                {"name": "currency0", "value": "0x0000000000000000000000000000000000000001"},
                {"name": "currency1", "value": "0x0000000000000000000000000000000000000002"},
                {"name": "fee", "value": "3000"},
                {"name": "tickSpacing", "value": "-60"},
                {"name": "hooks", "value": "0x0000000000000000000000000000000000000000"},
                {"name": "sqrtPriceX96", "value": "123"},
                {"name": "tick", "value": "-12"},
            ],
        },
    }
    result = parse_initialize(event)
    assert result is not None
    assert result["pool_id"] == "0xabcd"
    assert result["tick_spacing"] == -60
    assert result["source"] == {
        "block_number": 42,
        "block_hash": "0xblock",
        "log_index": 7,
        "transaction_hash": "0xtx",
        "block_timestamp": "2026-10-22T16:20:44.000000Z",
    }


def test_parse_initialize_rejects_incomplete_logs() -> None:
    assert parse_initialize({"decoded": {"abi": {"name": "Initialize"}, "parameters": []}}) is None


class Response:
    def __init__(self, status=200, payload=None, error=None):
        self.status, self.payload, self.error = status, payload, error
    async def __aenter__(self):
        if self.error: raise self.error
        return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs): return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.cursors = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, endpoint, params):
        self.cursors.append(dict(params))
        return next(self.responses)


def test_timeout_and_503_retry_same_page_without_losing_cursor(monkeypatch):
    delays = []
    async def sleep(delay): delays.append(delay)
    monkeypatch.setattr(v4_inventory.asyncio, 'sleep', sleep)
    page = {'items': [], 'next_page_params': {'block_number': 10}}
    session = Session([Response(error=asyncio.TimeoutError()), Response(503), Response(payload=page)])
    cursor = {'block_number': 20}
    result = asyncio.run(v4_inventory._get_page(session, 'https://example.test', cursor))
    assert result == page and session.cursors == [cursor, cursor, cursor]
    assert delays == [1, 2]


@pytest.mark.parametrize('status', [401, 403, 404])
def test_permanent_http_failure_is_not_retried(status):
    session = Session([Response(status)])
    with pytest.raises(RuntimeError, match=str(status)):
        asyncio.run(v4_inventory._get_page(session, 'https://example.test', {}))
    assert len(session.cursors) == 1


@pytest.mark.parametrize('cursor', ['invalid', {'block_number': 20}])
def test_invalid_or_repeated_cursor_is_rejected(cursor):
    session = Session([Response(payload={'items': [], 'next_page_params': cursor})])
    with pytest.raises(RuntimeError, match='cursor'):
        asyncio.run(v4_inventory._get_page(session, 'https://example.test', {'block_number': 20}))


def test_failed_next_page_preserves_checkpoint_and_resume_continues_from_it(tmp_path, monkeypatch):
    cursor = {'block_number': 20, 'index': 1}
    session = Session([Response(payload={'items': [], 'next_page_params': cursor}),
                       Response(error=asyncio.TimeoutError()), Response(503)])
    monkeypatch.setattr(v4_inventory.aiohttp, 'ClientSession', lambda **kwargs: session)
    async def sleep(delay): pass
    monkeypatch.setattr(v4_inventory.asyncio, 'sleep', sleep)
    config = v4_inventory.V4DiscoveryConfig(tmp_path/'checkpoint.json', max_pages=2, attempts=2)
    with pytest.raises(RuntimeError, match='2 attempts'):
        asyncio.run(v4_inventory.discover_v4_pools(config))
    checkpoint = json.loads(config.output.read_text())
    assert checkpoint['pages_scanned'] == 1 and checkpoint['complete'] is False
    assert checkpoint['next_page_params'] == cursor
    resumed = Session([Response(payload={'items': [], 'next_page_params': None})])
    monkeypatch.setattr(v4_inventory.aiohttp, 'ClientSession', lambda **kwargs: resumed)
    result = asyncio.run(v4_inventory.discover_v4_pools(config))
    assert resumed.cursors == [cursor]
    assert result['pages_scanned'] == 2 and result['complete'] is True
