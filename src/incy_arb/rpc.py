"""Small read-only RPC client used by the observer."""
from __future__ import annotations

import asyncio
import itertools
from typing import Any

import aiohttp

from .abi import MULTICALL3, ReadCall, decode_aggregate3, encode_aggregate3


class RpcError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class ReadOnlyRpc:
    """Only implements sealed-block reads; no generic call or send method exists."""

    def __init__(self, url: str, timeout_seconds: float = 8.0, requests_per_second: float = 3.0) -> None:
        self.url = url
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.requests_per_second = requests_per_second
        self._ids = itertools.count(1)
        self._session: aiohttp.ClientSession | None = None
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> "ReadOnlyRpc":
        # Cloud environments commonly expose HTTPS through proxy variables.
        # Respecting them also avoids a direct, non-reproducible egress path.
        self._session = aiohttp.ClientSession(timeout=self.timeout, trust_env=True)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._session:
            await self._session.close()

    async def sealed_block(self, block_number: int, attempts: int = 6) -> dict[str, Any] | None:
        last_error: RpcError | None = None
        for attempt in range(attempts):
            try:
                result = await self._request("eth_getBlockByNumber", [hex(block_number), False])
                if result is not None:
                    return result
            except RpcError as exc:
                last_error = exc
            await asyncio.sleep(0.2 * (attempt + 1))
        if last_error is not None:
            raise last_error
        return None

    async def chain_id(self) -> int:
        return int(await self._request("eth_chainId", []), 16)

    async def code_at(self, address: str, block: int) -> str:
        return await self._request("eth_getCode", [address, hex(block)])

    async def simulation_call(self, transaction: dict[str, Any], block: int, overrides: dict[str, Any]) -> str:
        """Execute a hypothetical transaction without signing, broadcasting, or persisting state."""
        return await self._request("eth_call", [transaction, hex(block), overrides])

    async def simulation_gas(self, transaction: dict[str, Any], block: int, overrides: dict[str, Any]) -> int:
        return int(await self._request("eth_estimateGas", [transaction, hex(block), overrides]), 16)

    async def block_number(self) -> int:
        return int(await self._request("eth_blockNumber", []), 16)

    async def contract_call(self, target: str, data: bytes, block: int | str = "latest", attempts: int = 4) -> str:
        tag = hex(block) if isinstance(block, int) else block
        last_error: RpcError | None = None
        for attempt in range(attempts):
            try:
                result = await self._request("eth_call", [{"to": target, "data": "0x" + data.hex()}, tag])
                if isinstance(result, str):
                    return result
                raise RpcError("eth_call returned non-hex result")
            except RpcError as exc:
                last_error = exc
                if exc.code in (3, -32601, -32602):
                    raise  # Execution reverts and invalid/unsupported methods are deterministic.
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.3 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def multicall(self, calls: list[ReadCall], block: int | str) -> list[tuple[bool, bytes]]:
        if not calls:
            return []
        result = await self.contract_call(MULTICALL3, encode_aggregate3(calls), block)
        decoded = decode_aggregate3(result)
        if len(decoded) != len(calls):
            raise RpcError("Multicall3 returned wrong number of results")
        return decoded

    async def _request(self, method: str, params: list[Any]) -> Any:
        if self._session is None:
            raise RuntimeError("RPC session is not started")
        await self._take_rate_slot()
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            async with self._session.post(self.url, json=body) as response:
                if response.status != 200:
                    raise RpcError(f"RPC HTTP {response.status}")
                reply = await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RpcError(f"RPC transport error: {exc}") from exc
        if "error" in reply:
            error = reply["error"]
            raise RpcError(str(error.get("message", "RPC error")), code=error.get("code"), data=error.get("data"))
        return reply.get("result")

    async def _take_rate_slot(self) -> None:
        if self.requests_per_second <= 0:
            return
        async with self._rate_lock:
            now = asyncio.get_running_loop().time()
            wait = self._next_request_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request_at = asyncio.get_running_loop().time() + 1 / self.requests_per_second


class PinnedQuoteCache:
    """Bounded cache for independent quotes at one sealed block, never simulation overrides."""

    def __init__(self, rpc: ReadOnlyRpc, block: int, max_entries: int = 20_000) -> None:
        if block < 0 or max_entries < 1:
            raise ValueError("nonnegative pinned block and positive cache limit required")
        self.rpc, self.block, self.max_entries = rpc, block, max_entries
        self.entries: dict[tuple[str, bytes], str | RpcError] = {}
        self.hits = 0
        self.misses = 0

    async def contract_call(self, target: str, data: bytes, block: int | str = "latest", attempts: int = 4) -> str:
        if block != self.block:
            raise ValueError("quote cache accepts only its pinned sealed block")
        key = (target.lower(), data)
        if key in self.entries:
            self.hits += 1
            cached = self.entries[key]
            if isinstance(cached, RpcError):
                raise RpcError(str(cached), code=cached.code, data=cached.data)
            return cached
        self.misses += 1
        try:
            value = await self.rpc.contract_call(target, data, block, attempts)
        except RpcError as error:
            if error.code == 3 and len(self.entries) < self.max_entries:
                self.entries[key] = RpcError(str(error), code=error.code, data=error.data)
            raise
        if len(self.entries) < self.max_entries:
            self.entries[key] = value
        return value

    def summary(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "entries": len(self.entries), "max_entries": self.max_entries}
