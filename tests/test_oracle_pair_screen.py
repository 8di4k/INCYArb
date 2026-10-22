import asyncio
import importlib.util
from pathlib import Path

import pytest
from eth_abi import decode, encode

from incy_arb.rpc import RpcError

spec = importlib.util.spec_from_file_location(
    "oracle_pair_screen", Path(__file__).parents[1] / "scripts/screen_oracle_pair.py"
)
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)


def test_runs_split_unknowns_and_unsampled_blocks_without_inventing_duration():
    rows = [
        {"block": b, "timestamp": b * 2, "difference_raw": value}
        for b, value in [(1, "1"), (2, "2"), (3, None), (4, "1"), (6, "1"), (7, "0")]
    ]
    runs = screen.positive_runs(rows)
    assert [(r["first_block"], r["samples"], r["observed_span_seconds"]) for r in runs] == [
        (1, 2, 2),
        (4, 1, 0),
        (6, 1, 0),
    ]


@pytest.fixture
def rpc(monkeypatch):
    class Rpc:
        replace = False
        fail = False
        malformed = False
        calls = []

        def __init__(self, *args, **kwargs):
            self.headers = 0
            type(self).calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return 8453

        async def code_at(self, address, block):
            return "0x6000"

        async def sealed_block(self, block):
            self.headers += 1
            return {
                "number": hex(block),
                "timestamp": hex(block * 2),
                "hash": "replaced" if self.replace and self.headers > 1 else str(block),
            }

        async def simulation_call(self, transaction, block, overrides):
            assert overrides == {}
            assert transaction["from"] == "0x" + "12" * 20
            token_in, token_out, amount = decode(
                ["address", "address", "uint256"], bytes.fromhex(transaction["data"][10:])
            )
            self.calls.append((token_in, token_out, amount, block))
            if self.fail:
                raise RpcError("HTTP 429")
            if self.malformed:
                return "0x01"
            output = 37 if token_in == screen.USDC else 101_000000
            return "0x" + encode(["uint256"], [output]).hex()

    monkeypatch.setattr(screen, "ReadOnlyRpc", Rpc)
    return Rpc


def run():
    return asyncio.run(screen.screen(10, 10, 1, 100_000000, "0x" + "12" * 20, "unused"))


def test_second_leg_uses_actual_first_output_at_the_same_block(rpc):
    result = run()
    assert [c[2] for c in rpc.calls] == [100_000000, 37, 100_000000, 37]
    assert all(c[3] == 10 for c in rpc.calls)
    assert all(r["difference_raw"] == "1000000" for r in result["cycles"])
    assert result["execution_verified"] is False
    assert result["oracle_publish_lag_verified"] is False


@pytest.mark.parametrize("mode,status", [("fail", "rpc_error"), ("malformed", "malformed_response")])
def test_unknown_quotes_skip_second_leg_and_do_not_become_losses(rpc, mode, status):
    setattr(rpc, mode, True)
    result = run()
    assert len(rpc.calls) == 2
    assert all(r["difference_raw"] is None for r in result["cycles"])
    assert all(r["status"] == status for r in result["reads"])
    assert not any(result["positive_snapshot_runs"].values())


def test_changed_historical_block_rejects_report(rpc):
    rpc.replace = True
    with pytest.raises(RuntimeError, match="replaced"):
        run()
