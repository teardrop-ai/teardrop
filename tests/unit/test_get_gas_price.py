"""Unit tests for tools/definitions/get_gas_price.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestNextBaseFee:
    def test_increases_when_full_block(self):
        from tools.definitions.get_gas_price import _next_base_fee

        base_fee = 10_000_000_000  # 10 gwei
        gas_limit = 15_000_000
        result = _next_base_fee(base_fee, gas_limit, gas_limit)
        assert result > base_fee

    def test_decreases_when_empty_block(self):
        from tools.definitions.get_gas_price import _next_base_fee

        base_fee = 10_000_000_000
        result = _next_base_fee(base_fee, 0, 15_000_000)
        assert result < base_fee

    def test_unchanged_at_target(self):
        from tools.definitions.get_gas_price import _next_base_fee

        base_fee = 10_000_000_000
        gas_limit = 15_000_000
        gas_target = gas_limit // 2
        result = _next_base_fee(base_fee, gas_target, gas_limit)
        assert result == base_fee

    def test_cannot_go_below_zero(self):
        from tools.definitions.get_gas_price import _next_base_fee

        result = _next_base_fee(1, 0, 15_000_000)
        assert result >= 0


class TestGetGasPrice:
    def _mock_block(self, base_fee=10_000_000_000, gas_used=7_500_000, gas_limit=15_000_000):
        return {
            "baseFeePerGas": base_fee,
            "gasUsed": gas_used,
            "gasLimit": gas_limit,
        }

    async def test_returns_all_eip1559_fields(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        monkeypatch.setattr("tools.definitions.get_gas_price._GAS_CACHE", {})

        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value=self._mock_block())

        monkeypatch.setattr("tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3)

        with patch(
            "tools.definitions.get_gas_price._get_max_priority_fee",
            AsyncMock(return_value=1_000_000_000),
        ):
            result = await get_gas_price(chain_id=1)

        assert result["chain_id"] == 1
        assert result["base_fee_gwei"] is not None
        assert result["priority_fee_gwei"] is not None
        assert result["next_base_fee_gwei"] is not None
        assert result["gas_used_ratio"] is not None
        assert 0.0 <= result["gas_used_ratio"] <= 1.0

    async def test_gas_price_is_base_plus_priority(self, test_settings, monkeypatch):
        from web3 import Web3

        from tools.definitions.get_gas_price import get_gas_price

        monkeypatch.setattr("tools.definitions.get_gas_price._GAS_CACHE", {})

        base_fee = 10_000_000_000
        priority = 1_000_000_000

        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value=self._mock_block(base_fee=base_fee))

        monkeypatch.setattr("tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3)

        with patch(
            "tools.definitions.get_gas_price._get_max_priority_fee",
            AsyncMock(return_value=priority),
        ):
            result = await get_gas_price(chain_id=1)

        expected = str(Web3.from_wei(base_fee + priority, "gwei"))
        assert result["gas_price_gwei"] == expected

    async def test_result_is_cached(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        monkeypatch.setattr("tools.definitions.get_gas_price._GAS_CACHE", {})

        call_count = 0

        async def counting_get_block(identifier):
            nonlocal call_count
            call_count += 1
            return self._mock_block()

        mock_w3 = MagicMock()
        mock_w3.eth.get_block = counting_get_block

        monkeypatch.setattr("tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3)

        with patch(
            "tools.definitions.get_gas_price._get_max_priority_fee",
            AsyncMock(return_value=1_000_000_000),
        ):
            await get_gas_price(chain_id=1)
            await get_gas_price(chain_id=1)

        assert call_count == 1

    async def test_missing_priority_fee_does_not_raise(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        monkeypatch.setattr("tools.definitions.get_gas_price._GAS_CACHE", {})

        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value=self._mock_block())

        monkeypatch.setattr("tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3)

        with patch(
            "tools.definitions.get_gas_price._get_max_priority_fee",
            AsyncMock(return_value=None),
        ):
            result = await get_gas_price(chain_id=1)

        assert result["priority_fee_gwei"] is None

    async def test_no_base_fee_falls_back_to_legacy_gas_price(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        monkeypatch.setattr("tools.definitions.get_gas_price._GAS_CACHE", {})

        legacy_price = 20_000_000_000

        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value={"gasUsed": 0, "gasLimit": 15_000_000})

        # Simulate legacy eth_gasPrice as an awaitable coroutine.
        async def _fake_gas_price():
            return legacy_price

        mock_w3.eth.gas_price = _fake_gas_price()

        monkeypatch.setattr("tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3)

        with patch(
            "tools.definitions.get_gas_price._get_max_priority_fee",
            AsyncMock(return_value=None),
        ):
            result = await get_gas_price(chain_id=1)

        assert result["base_fee_gwei"] is None
        assert result["next_base_fee_gwei"] is None
        assert result["gas_price_gwei"] is not None

    async def test_base_chain(self, test_settings, monkeypatch):
        from tools.definitions.get_gas_price import get_gas_price

        monkeypatch.setattr("tools.definitions.get_gas_price._GAS_CACHE", {})

        mock_w3 = MagicMock()
        mock_w3.eth.get_block = AsyncMock(return_value=self._mock_block(base_fee=100_000_000))

        monkeypatch.setattr("tools.definitions.get_gas_price.get_web3", lambda chain_id=1: mock_w3)

        with patch(
            "tools.definitions.get_gas_price._get_max_priority_fee",
            AsyncMock(return_value=0),
        ):
            result = await get_gas_price(chain_id=8453)

        assert result["chain_id"] == 8453
