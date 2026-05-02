from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from tools.definitions import _web3_helpers as web3_helpers


@pytest.mark.asyncio
async def test_rpc_call_uses_chain_context_when_chain_id_passed(monkeypatch):
    seen_chain_ids: list[int | None] = []

    @asynccontextmanager
    async def _noop_rpc_sem():
        yield

    @asynccontextmanager
    async def _noop_chain_sem(chain_id):
        seen_chain_ids.append(chain_id)
        yield

    monkeypatch.setattr(web3_helpers, "acquire_rpc_semaphore", _noop_rpc_sem)
    monkeypatch.setattr(web3_helpers, "acquire_chain_semaphore", _noop_chain_sem)

    async def _ok():
        return 42

    result = await web3_helpers.rpc_call(_ok, timeout_seconds=1, chain_id=8453)

    assert result == 42
    assert seen_chain_ids == [8453]


@pytest.mark.asyncio
async def test_rpc_call_retries_rate_limit_in_outer_wrapper(monkeypatch):
    @asynccontextmanager
    async def _noop_rpc_sem():
        yield

    @asynccontextmanager
    async def _noop_chain_sem(chain_id):
        yield

    monkeypatch.setattr(web3_helpers, "acquire_rpc_semaphore", _noop_rpc_sem)
    monkeypatch.setattr(web3_helpers, "acquire_chain_semaphore", _noop_chain_sem)
    monkeypatch.setattr(web3_helpers.asyncio, "sleep", AsyncMock())

    attempts = {"count": 0}

    async def _flaky():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise Exception("429 too many requests")
        return "ok"

    result = await web3_helpers.rpc_call(_flaky, timeout_seconds=1, chain_id=1)

    assert result == "ok"
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_retry_provider_does_not_retry_internally(monkeypatch):
    provider = web3_helpers._RetryAsyncHTTPProvider("http://example-rpc.local")

    parent_make_request = AsyncMock(side_effect=Exception("429 too many requests"))
    monkeypatch.setattr("web3.providers.AsyncHTTPProvider.make_request", parent_make_request)

    with pytest.raises(Exception, match="429"):
        await provider.make_request("eth_call", [])

    assert parent_make_request.call_count == 1
