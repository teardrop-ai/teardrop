"""Unit tests for tools/definitions/get_lending_rates.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import encode as abi_encode

import tools.definitions.get_lending_rates as glr
from tools.definitions.get_lending_rates import get_lending_rates


def _mock_w3(block_number: int = 12345) -> MagicMock:
    mock = MagicMock()

    class _EthProxy:
        @property
        def block_number(self):
            async def _bn():
                return block_number

            return _bn()

    mock.eth = _EthProxy()
    return mock


class TestHelpers:
    def test_ray_to_apy_pct(self):
        # 5% annualized in ray units.
        assert glr._ray_to_apy_pct(int(0.05 * 1e27)) == pytest.approx(5.0, rel=1e-5)

    def test_per_second_rate_to_apy_pct(self):
        # Small positive per-second rate should produce positive APY.
        apy = glr._per_second_rate_to_apy_pct(1_000_000_000)
        assert apy > 0


class TestGetLendingRates:
    async def test_unsupported_chain_raises(self, test_settings):
        with pytest.raises(ValueError, match="Unsupported chain_id"):
            await get_lending_rates(chain_id=137)

    async def test_unsupported_protocol_raises(self, test_settings):
        with pytest.raises(ValueError, match="Unsupported protocol"):
            await get_lending_rates(protocol="spark")

    async def test_aave_rates_happy_path(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_lending_rates._rates_cache", {})
        monkeypatch.setattr("tools.definitions.get_lending_rates.get_web3", lambda _chain_id=1: _mock_w3(42))

        async def _rpc_call(coro_fn, timeout_seconds=None, chain_id=None):
            return await coro_fn()

        monkeypatch.setattr("tools.definitions.get_lending_rates.rpc_call", _rpc_call)
        monkeypatch.setattr(
            "tools.definitions.get_lending_rates._AAVE_V3_DATA_PROVIDER",
            {1: "0x0a16f2FCC0D44FaE41cc54e079281D84A363bECD"},
        )
        monkeypatch.setattr(
            "tools.definitions.get_lending_rates._AAVE_V3_TRACKED_RESERVES",
            {
                1: [
                    {
                        "symbol": "USDC",
                        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                        "decimals": "6",
                    }
                ]
            },
        )

        reserve_data = abi_encode(
            [
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "uint40",
            ],
            [0, 0, 0, int(0.05 * 1e27), int(0.07 * 1e27), 0, 0, 0, 0, 0],
        )
        monkeypatch.setattr(
            "tools.definitions.get_lending_rates.multicall3_batch",
            AsyncMock(return_value=[(True, reserve_data)]),
        )

        result = await get_lending_rates(protocol="aave-v3", chain_id=1, assets=["usdc"])

        assert result["data_block_number"] == 42
        assert result["errors"] == []
        assert len(result["rates"]) == 1
        assert result["rates"][0]["protocol"] == "aave-v3"
        assert result["rates"][0]["asset_symbol"] == "USDC"
        assert result["rates"][0]["supply_apy_pct"] == pytest.approx(5.0, rel=1e-5)
        assert result["rates"][0]["borrow_apy_pct"] == pytest.approx(7.0, rel=1e-5)

    async def test_compound_rates_happy_path(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_lending_rates._rates_cache", {})
        monkeypatch.setattr("tools.definitions.get_lending_rates.get_web3", lambda _chain_id=1: _mock_w3(77))

        async def _rpc_call(coro_fn, timeout_seconds=None, chain_id=None):
            return await coro_fn()

        monkeypatch.setattr("tools.definitions.get_lending_rates.rpc_call", _rpc_call)
        monkeypatch.setattr(
            "tools.definitions.get_lending_rates._COMPOUND_V3_MARKETS",
            {
                1: [
                    {
                        "name": "cUSDCv3",
                        "address": "0xc3d688B66703497DAA19211EEdff47f25384cdc3",
                        "base_symbol": "USDC",
                        "base_decimals": "6",
                        "collateral_assets": [],
                    }
                ]
            },
        )

        util_result = [(True, abi_encode(["uint256"], [int(0.82 * 1e18)]))]
        rates_result = [
            (True, abi_encode(["uint256"], [1_500_000_000])),
            (True, abi_encode(["uint256"], [2_800_000_000])),
        ]
        monkeypatch.setattr(
            "tools.definitions.get_lending_rates.multicall3_batch",
            AsyncMock(side_effect=[util_result, rates_result]),
        )

        result = await get_lending_rates(protocol="compound-v3", chain_id=1, assets=["USDC"])

        assert result["data_block_number"] == 77
        assert result["errors"] == []
        assert len(result["rates"]) == 1
        row = result["rates"][0]
        assert row["protocol"] == "compound-v3"
        assert row["asset_symbol"] == "USDC"
        assert row["utilization_pct"] == pytest.approx(82.0)
        assert row["supply_apy_pct"] > 0
        assert row["borrow_apy_pct"] > 0

    async def test_cache_hit_skips_refetch(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_lending_rates._rates_cache", {})
        monkeypatch.setattr("tools.definitions.get_lending_rates.get_web3", lambda _chain_id=1: _mock_w3(88))

        async def _rpc_call(coro_fn, timeout_seconds=None, chain_id=None):
            return await coro_fn()

        monkeypatch.setattr("tools.definitions.get_lending_rates.rpc_call", _rpc_call)

        fetch_mock = AsyncMock(
            return_value=[
                glr.LendingRateEntry(
                    protocol="aave-v3",
                    chain_id=1,
                    market_name="Aave v3",
                    asset_symbol="USDC",
                    supply_apy_pct=4.2,
                    borrow_apy_pct=6.8,
                )
            ]
        )
        monkeypatch.setattr("tools.definitions.get_lending_rates._fetch_aave_rates", fetch_mock)

        first = await get_lending_rates(protocol="aave-v3", chain_id=1)
        second = await get_lending_rates(protocol="aave-v3", chain_id=1)

        assert fetch_mock.await_count == 1
        assert first == second

    async def test_partial_protocol_failure_records_error(self, test_settings, monkeypatch):
        monkeypatch.setattr("tools.definitions.get_lending_rates._rates_cache", {})
        monkeypatch.setattr("tools.definitions.get_lending_rates.get_web3", lambda _chain_id=1: _mock_w3(99))

        async def _rpc_call(coro_fn, timeout_seconds=None, chain_id=None):
            return await coro_fn()

        monkeypatch.setattr("tools.definitions.get_lending_rates.rpc_call", _rpc_call)
        monkeypatch.setattr("tools.definitions.get_lending_rates._fetch_aave_rates", AsyncMock(side_effect=Exception("boom")))
        monkeypatch.setattr("tools.definitions.get_lending_rates._fetch_compound_rates", AsyncMock(return_value=[]))

        result = await get_lending_rates(protocol="all", chain_id=1)

        assert "aave-v3 unavailable" in result["errors"]


class TestRegistration:
    def test_tool_registered_in_all_tools(self):
        from tools.definitions import _ALL_TOOLS

        names = {t.name for t in _ALL_TOOLS}
        assert "get_lending_rates" in names
