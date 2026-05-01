# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2026 Teardrop AI. All rights reserved.
"""Unit tests for tools/definitions/get_dex_quote.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from web3.exceptions import ContractLogicError

# Checksummed addresses used across tests.
_WETH_ETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_WETH_BASE = "0x4200000000000000000000000000000000000006"
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Arbitrary unknown-to-static-map token (valid checksum but not in _KNOWN_DECIMALS).
_UNKNOWN_TOKEN = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"  # PEPE


# ─── Input validation ─────────────────────────────────────────────────────────


class TestGetDexQuoteInput:
    def test_valid_minimal(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        obj = GetDexQuoteInput(token_in=_WETH_ETH, token_out=_USDC_ETH, amount_in="1000000000000000000")
        assert obj.chain_id == 1

    def test_non_checksum_address_rejected(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        with pytest.raises(ValidationError, match="checksummed"):
            GetDexQuoteInput(
                token_in=_WETH_ETH.lower(),
                token_out=_USDC_ETH,
                amount_in="1",
            )

    def test_zero_amount_rejected(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        with pytest.raises(ValidationError, match="> 0"):
            GetDexQuoteInput(token_in=_WETH_ETH, token_out=_USDC_ETH, amount_in="0")

    def test_negative_amount_rejected(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        with pytest.raises(ValidationError, match="> 0"):
            GetDexQuoteInput(token_in=_WETH_ETH, token_out=_USDC_ETH, amount_in="-5")

    def test_non_numeric_amount_rejected(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        with pytest.raises(ValidationError, match="uint256"):
            GetDexQuoteInput(token_in=_WETH_ETH, token_out=_USDC_ETH, amount_in="abc")

    def test_amount_over_cap_rejected(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        with pytest.raises(ValidationError, match="2\\^128"):
            GetDexQuoteInput(
                token_in=_WETH_ETH,
                token_out=_USDC_ETH,
                amount_in=str(2**128),
            )

    def test_unsupported_chain_rejected(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        with pytest.raises(ValidationError, match="Unsupported chain_id"):
            GetDexQuoteInput(
                token_in=_WETH_ETH,
                token_out=_USDC_ETH,
                amount_in="1",
                chain_id=137,
            )

    def test_base_chain_accepted(self):
        from tools.definitions.get_dex_quote import GetDexQuoteInput

        obj = GetDexQuoteInput(
            token_in=_WETH_BASE,
            token_out=_USDC_BASE,
            amount_in="1000000000000000000",
            chain_id=8453,
        )
        assert obj.chain_id == 8453


# ─── _resolve_decimals ────────────────────────────────────────────────────────


class TestResolveDecimals:
    async def test_static_map_hit_no_rpc(self, test_settings):
        from tools.definitions.get_dex_quote import _resolve_decimals

        mock_w3 = MagicMock()
        # Raise if eth.contract is touched — static hit must not use RPC.
        mock_w3.eth.contract.side_effect = AssertionError("RPC used despite static hit")

        result = await _resolve_decimals(mock_w3, 1, _USDC_ETH)
        assert result == 6

    async def test_fallback_rpc_call(self, test_settings, monkeypatch):
        import tools.definitions.get_dex_quote as mod

        # Ensure unknown token has no cache entry from another test.
        mod._decimals_cache.clear()

        mock_w3 = MagicMock()
        mock_contract = MagicMock()
        mock_contract.functions.decimals.return_value.call = AsyncMock(return_value=18)
        mock_w3.eth.contract.return_value = mock_contract

        result = await mod._resolve_decimals(mock_w3, 1, _UNKNOWN_TOKEN)
        assert result == 18

    async def test_fallback_on_rpc_failure_defaults_to_18(self, test_settings):
        import tools.definitions.get_dex_quote as mod

        mod._decimals_cache.clear()

        mock_w3 = MagicMock()
        mock_contract = MagicMock()
        mock_contract.functions.decimals.return_value.call = AsyncMock(side_effect=Exception("rpc timeout"))
        mock_w3.eth.contract.return_value = mock_contract

        result = await mod._resolve_decimals(mock_w3, 1, _UNKNOWN_TOKEN)
        assert result == 18


# ─── Main function ────────────────────────────────────────────────────────────


class TestGetDexQuote:
    def _make_mock_w3(self, quote_results_by_fee: dict[int, object]) -> MagicMock:
        """Build a mock Web3 returning tier-specific quoter results.

        ``quote_results_by_fee`` maps fee tier (int) → either a 4-tuple of
        (amountOut, sqrtPriceX96After, ticksCrossed, gasEstimate) for a
        successful quote, or an Exception instance to be raised.
        """
        import asyncio as _asyncio

        mock_w3 = MagicMock()
        # web3.py's AsyncEth exposes block_number as an awaitable property; the
        # module awaits it exactly once per invocation, so assigning a single
        # coroutine instance is sufficient for each test.
        mock_w3.eth.block_number = _asyncio.sleep(0, result=22_000_000)

        def _contract_factory(*args, **kwargs):
            c = MagicMock()
            # decimals() fallback for any token not covered by the static map.
            c.functions.decimals.return_value.call = AsyncMock(return_value=18)

            # Route quoter calls by fee tier (4th field in the params tuple).
            def _qei(params):
                fee = params[3]
                outcome = quote_results_by_fee.get(fee)
                stub = MagicMock()
                if isinstance(outcome, Exception):
                    stub.call = AsyncMock(side_effect=outcome)
                elif outcome is None:
                    stub.call = AsyncMock(side_effect=ContractLogicError("execution reverted: Unexpected error"))
                else:
                    stub.call = AsyncMock(return_value=outcome)
                return stub

            c.functions.quoteExactInputSingle.side_effect = _qei
            return c

        mock_w3.eth.contract.side_effect = _contract_factory
        return mock_w3

    async def test_all_tiers_revert_returns_no_liquidity(self, test_settings, monkeypatch):
        from tools.definitions.get_dex_quote import get_dex_quote

        mock_w3 = self._make_mock_w3({})  # every tier → pool_not_found
        monkeypatch.setattr("tools.definitions.get_dex_quote.get_web3", lambda chain_id=1: mock_w3)

        result = await get_dex_quote(
            token_in=_WETH_ETH,
            token_out=_USDC_ETH,
            amount_in="1000000000000000000",
            chain_id=1,
        )

        assert result["no_liquidity"] is True
        assert result["fee_tier_used"] is None
        assert result["amount_out"] == "0"
        assert len(result["quotes_per_tier"]) == 4
        assert all(q["success"] is False for q in result["quotes_per_tier"])
        assert all(q["error"] == "pool_not_found" for q in result["quotes_per_tier"])

    async def test_best_tier_selected_from_multiple(self, test_settings, monkeypatch):
        from tools.definitions.get_dex_quote import get_dex_quote

        # 500 bps returns 3200 USDC; 3000 bps returns 3180 USDC; others revert.
        mock_w3 = self._make_mock_w3(
            {
                500: (3_200_000_000, 79228162514264337593543950336, 2, 120_000),
                3000: (3_180_000_000, 79228162514264337593543950336, 2, 120_000),
            }
        )
        monkeypatch.setattr("tools.definitions.get_dex_quote.get_web3", lambda chain_id=1: mock_w3)

        result = await get_dex_quote(
            token_in=_WETH_ETH,
            token_out=_USDC_ETH,
            amount_in="1000000000000000000",  # 1 WETH
            chain_id=1,
        )

        assert result["no_liquidity"] is False
        assert result["fee_tier_used"] == 500
        assert result["amount_out"] == "3200000000"
        assert result["amount_in_decimals"] == 18
        assert result["amount_out_decimals"] == 6
        assert result["amount_out_human"] == "3200"
        # 3200 USDC / 1 WETH → rate "3200"
        assert result["effective_rate"] == "3200"
        assert result["block_number"] == 22_000_000

        per_tier = {q["fee_tier"]: q for q in result["quotes_per_tier"]}
        assert per_tier[500]["success"] is True
        assert per_tier[3000]["success"] is True
        assert per_tier[100]["success"] is False
        assert per_tier[100]["error"] == "pool_not_found"
        assert per_tier[10000]["success"] is False

    async def test_same_token_rejected(self, test_settings, monkeypatch):
        from tools.definitions.get_dex_quote import get_dex_quote

        # get_web3 should never be called when validation fails early.
        monkeypatch.setattr(
            "tools.definitions.get_dex_quote.get_web3",
            lambda chain_id=1: (_ for _ in ()).throw(AssertionError("should not be called")),
        )

        with pytest.raises(ValueError, match="must differ"):
            await get_dex_quote(
                token_in=_WETH_ETH,
                token_out=_WETH_ETH,
                amount_in="1000",
                chain_id=1,
            )

    async def test_base_chain_uses_base_quoter(self, test_settings, monkeypatch):
        from tools.definitions.get_dex_quote import _QUOTER_V2, get_dex_quote

        mock_w3 = self._make_mock_w3({500: (3_200_000_000, 79228162514264337593543950336, 2, 120_000)})
        monkeypatch.setattr("tools.definitions.get_dex_quote.get_web3", lambda chain_id=8453: mock_w3)

        result = await get_dex_quote(
            token_in=_WETH_BASE,
            token_out=_USDC_BASE,
            amount_in="1000000000000000000",
            chain_id=8453,
        )

        assert result["chain_id"] == 8453
        assert result["fee_tier_used"] == 500
        # Verify Base QuoterV2 address was used via contract factory call args.
        contract_calls = mock_w3.eth.contract.call_args_list
        addresses_used = [call.kwargs.get("address") or (call.args[0] if call.args else None) for call in contract_calls]
        assert _QUOTER_V2[8453] in addresses_used

    async def test_uses_global_rpc_semaphore_for_quote_calls(self, test_settings, monkeypatch):
        import tools.definitions.get_dex_quote as mod

        mod._decimals_cache.clear()

        mock_w3 = self._make_mock_w3({500: (3_200_000_000, 79228162514264337593543950336, 2, 120_000)})
        monkeypatch.setattr("tools.definitions.get_dex_quote.get_web3", lambda chain_id=1: mock_w3)

        class _DummySem:
            async def __aenter__(self):
                sem_enters["count"] += 1

            async def __aexit__(self, exc_type, exc, tb):
                return False

        sem_enters = {"count": 0}
        monkeypatch.setattr("tools.definitions.get_dex_quote.acquire_rpc_semaphore", lambda: _DummySem())

        await mod.get_dex_quote(
            token_in=_UNKNOWN_TOKEN,  # unknown token forces decimals() RPC path
            token_out=_USDC_ETH,
            amount_in="1000000000000000000",
            chain_id=1,
        )

        # 1 decimals() + 4 fee-tier quote calls + 1 block_number call.
        assert sem_enters["count"] >= 6


# ─── ToolDefinition registration ──────────────────────────────────────────────


class TestToolRegistration:
    def test_tool_exported(self):
        from tools.definitions.get_dex_quote import TOOL

        assert TOOL.name == "get_dex_quote"
        assert TOOL.version == "1.0.0"
        assert "uniswap" in TOOL.tags
        assert TOOL.input_schema.__name__ == "GetDexQuoteInput"
        assert TOOL.output_schema.__name__ == "GetDexQuoteOutput"

    def test_tool_registered_in_package(self):
        from tools.definitions import _ALL_TOOLS
        from tools.definitions.get_dex_quote import TOOL

        assert TOOL in _ALL_TOOLS
