"""Unit tests for tools/definitions/get_gas_price.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


class _AwaitableValue:
    """A descriptor that returns an awaitable value, mimicking web3's async property."""
    def __init__(self, value):
        self._value = value
    def __await__(self):
        return self._make_coro().__await__()
    async def _make_coro(self):
        return self._value


class TestGetGasPrice:
    async def test_returns_gas_prices(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        mock_block = {
            "baseFeePerGas": 20_000_000_000,  # 20 gwei
        }
        mock_eth = MagicMock()
        mock_eth.gas_price = _AwaitableValue(25_000_000_000)
        mock_eth.get_block = AsyncMock(return_value=mock_block)

        mock_w3 = MagicMock()
        mock_w3.eth = mock_eth

        monkeypatch.setattr(
            "tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3
        )

        result = await get_gas_price(chain_id=1)

        assert result["chain_id"] == 1
        assert "gas_price_gwei" in result
        assert "base_fee_gwei" in result
        assert "priority_fee_gwei" in result

    async def test_base_chain(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        mock_block = {"baseFeePerGas": 100_000_000}  # 0.1 gwei
        mock_eth = MagicMock()
        mock_eth.gas_price = _AwaitableValue(200_000_000)
        mock_eth.get_block = AsyncMock(return_value=mock_block)

        mock_w3 = MagicMock()
        mock_w3.eth = mock_eth

        monkeypatch.setattr(
            "tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3
        )

        result = await get_gas_price(chain_id=8453)
        assert result["chain_id"] == 8453
