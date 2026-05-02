"""Unit tests for tools/definitions/get_wallet_portfolio.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from eth_abi import encode as abi_encode


class TestGetWalletPortfolio:
    def _make_mock_w3(self, eth_balance: int = 0) -> MagicMock:
        mock_w3 = MagicMock()
        mock_w3.eth.get_balance = AsyncMock(return_value=eth_balance)
        return mock_w3

    def _patch_erc20_batch(self, monkeypatch, balance_values):
        """Patch multicall3_batch to return specific per-token balances.

        ``balance_values`` may be a single int (same for all) or a list of ints
        matching the order of _TRACKED_TOKENS for the chain.
        """

        async def _mock_batch(w3, calls, *, allow_failure=True):
            if isinstance(balance_values, int):
                return [(True, abi_encode(["uint256"], [balance_values])) for _ in calls]
            results = []
            for i, _ in enumerate(calls):
                v = balance_values[i] if i < len(balance_values) else 0
                results.append((True, abi_encode(["uint256"], [v])))
            return results

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio.multicall3_batch", _mock_batch)

    async def test_returns_portfolio_with_eth(self, test_settings, monkeypatch):
        from tools.definitions.get_wallet_portfolio import get_wallet_portfolio

        # Reset price cache
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_cache", {})
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._portfolio_price_ts", 0.0)

        monkeypatch.setattr(
            "tools.definitions.get_wallet_portfolio.get_web3",
            lambda chain_id=1: self._make_mock_w3(2_000_000_000_000_000_000),  # 2 ETH
        )
        self._patch_erc20_batch(monkeypatch, 0)  # all ERC-20 balances = 0

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

        monkeypatch.setattr(
            "tools.definitions.get_wallet_portfolio.get_web3",
            lambda chain_id=1: self._make_mock_w3(0),
        )
        # First token in _TRACKED_TOKENS[1] is USDC — give it a non-zero balance.
        self._patch_erc20_batch(monkeypatch, [5_000_000_000] + [0] * 50)

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

        monkeypatch.setattr(
            "tools.definitions.get_wallet_portfolio.get_web3",
            lambda chain_id=1: self._make_mock_w3(100_000_000_000_000_000),  # 0.1 ETH
        )
        self._patch_erc20_batch(monkeypatch, 10_000_000_000)  # 10000 USDC for all tokens

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
        monkeypatch.setattr("tools.definitions.get_wallet_portfolio.get_web3", lambda chain_id=1: mock_w3)
        self._patch_erc20_batch(monkeypatch, 0)

        async def mock_fetch_prices(cg_ids):
            return {cid: 1.0 for cid in cg_ids}

        monkeypatch.setattr("tools.definitions.get_wallet_portfolio._fetch_prices", mock_fetch_prices)

        result = await get_wallet_portfolio(
            wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            chain_id=1,
        )

        assert result["holdings"][0]["symbol"] == "ETH"
        assert result["holdings"][0]["balance_formatted"] == "0.000000"
        assert result["fetch_errors"] == ["ETH balance unavailable (RPC/Rate-limit error)"]
