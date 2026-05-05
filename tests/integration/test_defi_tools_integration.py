"""Integration-style smoke tests for DeFi tool orchestration paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from tools.definitions.get_lending_rates import get_lending_rates
from tools.definitions.get_protocol_tvl import get_protocol_tvl


def _mock_w3(block_number: int = 999) -> MagicMock:
    mock = MagicMock()

    class _EthProxy:
        @property
        def block_number(self):
            async def _bn():
                return block_number

            return _bn()

    mock.eth = _EthProxy()
    return mock


def _mock_text_session(status: int, text: str) -> MagicMock:
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


@pytest.mark.anyio
async def test_get_protocol_tvl_404_graceful(test_settings, monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
    monkeypatch.setattr(
        "tools.definitions.get_protocol_tvl.get_defillama_session",
        AsyncMock(return_value=_mock_text_session(404, "")),
    )

    result = await get_protocol_tvl("missing-slug")

    assert result["current_tvl_usd"] is None
    assert "not found" in result["note"].lower()


@pytest.mark.anyio
async def test_get_protocol_tvl_payload_error_retry_then_success(test_settings, monkeypatch):
    monkeypatch.setattr("tools.definitions.get_protocol_tvl._tvl_cache", {})
    response = _mock_text_session(200, "321.0").get.return_value
    session = MagicMock()
    session.get = MagicMock(side_effect=[aiohttp.ClientPayloadError("boom"), response])
    monkeypatch.setattr("tools.definitions.get_protocol_tvl.get_defillama_session", AsyncMock(return_value=session))

    result = await get_protocol_tvl("aave")

    assert session.get.call_count == 2
    assert result["current_tvl_usd"] == pytest.approx(321.0)


@pytest.mark.anyio
async def test_get_lending_rates_marks_aave_unavailable_on_repeated_failure(test_settings, monkeypatch):
    monkeypatch.setattr("tools.definitions.get_lending_rates._rates_cache", {})
    monkeypatch.setattr("tools.definitions.get_lending_rates.get_web3", lambda _chain_id=1: _mock_w3(123))

    async def _rpc_call(coro_fn, timeout_seconds=None, chain_id=None):
        return await coro_fn()

    monkeypatch.setattr("tools.definitions.get_lending_rates.rpc_call", _rpc_call)
    monkeypatch.setattr(
        "tools.definitions.get_lending_rates._fetch_aave_rates",
        AsyncMock(side_effect=TimeoutError("rpc timeout")),
    )
    monkeypatch.setattr("tools.definitions.get_lending_rates._fetch_compound_rates", AsyncMock(return_value=[]))

    result = await get_lending_rates(protocol="all", chain_id=1)

    assert "aave-v3 unavailable" in result["errors"]
