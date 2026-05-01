"""Unit tests for tools/definitions/get_wallet_portfolio.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


class TestGetWalletPortfolio:
    async def test_returns_portfolio_with_eth(self, test_settings, monkeypatch):
        from tools.definitions.get_wallet_portfolio import get_wallet_portfolio

        # Reset price cache
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_cache", {})
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_ts", 0.0)

        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(return_value=2_000_000_000_000_000_000)  # 2 ETH

        # Mock ERC-20 balanceOf: all return 0 (only ETH holds value)
        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call = AsyncMock(return_value=0)
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio.get_web3", lambda chain_id=1: mock_w3)

        # Mock price fetch
        async def mock_fetch_prices(cg_ids):
            return {cid: 3000.0 if cid == "ethereum" else 1.0 for cid in cg_ids}

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._fetch_prices", mock_fetch_prices)

        result = await get_wallet_portfolio(
            wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            chain_id=1,
        )

        assert result["chain_id"] == 1
        assert result["total_value_usd"] > 0
        assert len(result["holdings"]) >= 1
        assert result["holdings"][0]["symbol"] == "ETH"

    async def test_includes_erc20_with_balance(self, test_settings, monkeypatch):
        from tools.definitions.get_wallet_portfolio import get_wallet_portfolio

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_cache", {})
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_ts", 0.0)

        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(return_value=0)

        # First ERC-20 (USDC) has balance, rest return 0
        call_count = 0

        async def balance_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return 5_000_000_000  # 5000 USDC (6 decimals)
            return 0

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call = AsyncMock(side_effect=balance_side_effect)
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio.get_web3", lambda chain_id=1: mock_w3)

        async def mock_fetch_prices(cg_ids):
            return {cid: 1.0 for cid in cg_ids}

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._fetch_prices", mock_fetch_prices)

        result = await get_wallet_portfolio(
            wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            chain_id=1,
        )

        # Should include at least the USDC entry (ETH is always included even with 0 balance)
        symbols = [h["symbol"] for h in result["holdings"]]
        assert "USDC" in symbols

    async def test_sorted_by_value(self, test_settings, monkeypatch):
        from tools.definitions.get_wallet_portfolio import get_wallet_portfolio

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_cache", {})
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_ts", 0.0)

        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(return_value=100_000_000_000_000_000)  # 0.1 ETH

        # All ERC-20s return a large USDC balance
        async def balance_side_effect(*a, **kw):
            return 10_000_000_000  # 10000 USDC

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call = AsyncMock(side_effect=balance_side_effect)
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio.get_web3", lambda chain_id=1: mock_w3)

        async def mock_fetch_prices(cg_ids):
            prices = {
                "ethereum": 3000.0,
                "usd-coin": 1.0,
                "tether": 1.0,
                "dai": 1.0,
                "weth": 3000.0,
                "wrapped-bitcoin": 65000.0,
            }
            return {cid: prices.get(cid, 0.0) for cid in cg_ids}

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._fetch_prices", mock_fetch_prices)

        result = await get_wallet_portfolio(
            wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            chain_id=1,
        )

        values = [h["value_usd"] for h in result["holdings"]]
        assert values == sorted(values, reverse=True)

    async def test_eth_balance_rpc_error_returns_fetch_error(self, test_settings, monkeypatch):
        from tools.definitions.get_wallet_portfolio import get_wallet_portfolio

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_cache", {})
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_ts", 0.0)

        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(side_effect=Exception("429 rate limit"))

        mock_contract = MagicMock()
        mock_contract.functions.balanceOf.return_value.call = AsyncMock(return_value=0)
        mock_w3.eth.contract.return_value = mock_contract

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio.get_web3", lambda chain_id=1: mock_w3)

        async def mock_fetch_prices(cg_ids):
            return {cid: 1.0 for cid in cg_ids}

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._fetch_prices", mock_fetch_prices)

        result = await get_wallet_portfolio(
            wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            chain_id=1,
        )

        assert result["holdings"][0]["symbol"] == "ETH"
        assert result["holdings"][0]["balance_formatted"] == "0.000000"
        assert result["fetch_errors"] == ["ETH balance unavailable (RPC error)"]
