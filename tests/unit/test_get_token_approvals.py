"""Unit tests for tools/definitions/get_token_approvals.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode
from pydantic import ValidationError

# Well-known addresses used across tests (checksummed).
_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
_TOKEN_USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
_SPENDER_UNISWAP = "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD"  # Uniswap Universal v1.2
_SPENDER_UNKNOWN = "0x1234567890123456789012345678901234567890"
_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# Unlimited approval value (2^256 - 1).
_UINT256_MAX = 2**256 - 1
# Bounded approval value (well below _UNLIMITED_THRESHOLD).
_BOUNDED = 1_000_000  # 1 USDC (6 decimals)


# ─── Input validation ─────────────────────────────────────────────────────────


class TestGetTokenApprovalsInput:
    def test_valid_minimal_input(self):
        from tools.definitions.get_token_approvals import GetTokenApprovalsInput

        obj = GetTokenApprovalsInput(wallet_address=_WALLET)
        assert obj.chain_id == 1
        assert obj.tokens is None
        assert obj.spenders is None

    def test_valid_explicit_tokens_and_spenders(self):
        from tools.definitions.get_token_approvals import GetTokenApprovalsInput

        obj = GetTokenApprovalsInput(
            wallet_address=_WALLET,
            chain_id=8453,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )
        assert obj.chain_id == 8453
        assert len(obj.tokens) == 1  # type: ignore[arg-type]
        assert len(obj.spenders) == 1  # type: ignore[arg-type]

    def test_tokens_list_too_long_rejected(self):
        from tools.definitions.get_token_approvals import GetTokenApprovalsInput

        with pytest.raises(ValidationError, match="50"):
            GetTokenApprovalsInput(
                wallet_address=_WALLET,
                tokens=[_TOKEN_USDC] * 51,
            )

    def test_spenders_list_too_long_rejected(self):
        from tools.definitions.get_token_approvals import GetTokenApprovalsInput

        with pytest.raises(ValidationError, match="20"):
            GetTokenApprovalsInput(
                wallet_address=_WALLET,
                spenders=[_SPENDER_UNISWAP] * 21,
            )


# ─── Risk level helper ────────────────────────────────────────────────────────


class TestRiskLevel:
    def test_unlimited_unknown_spender_is_high(self):
        from tools.definitions.get_token_approvals import _risk_level

        assert _risk_level(is_unlimited=True, spender_name=None) == "high"

    def test_unlimited_known_spender_is_medium(self):
        from tools.definitions.get_token_approvals import _risk_level

        assert _risk_level(is_unlimited=True, spender_name="Uniswap v3 SwapRouter") == "medium"

    def test_bounded_any_spender_is_low(self):
        from tools.definitions.get_token_approvals import _risk_level

        assert _risk_level(is_unlimited=False, spender_name=None) == "low"
        assert _risk_level(is_unlimited=False, spender_name="Aave v3 Pool") == "low"


# ─── Main function ────────────────────────────────────────────────────────────


class TestGetTokenApprovals:
    def _patch_batch(self, monkeypatch, allowance_value: int, *, fail: bool = False, fail_index: int | None = None):
        """Patch multicall3_batch to return controlled allowance/failure outputs."""

        async def _mock_batch(w3, calls, *, allow_failure=True, chain_id=None):
            if fail:
                return [(False, b"")] * len(calls)
            out = []
            for idx, _ in enumerate(calls):
                if fail_index is not None and idx == fail_index:
                    out.append((False, b""))
                else:
                    out.append((True, abi_encode(["uint256"], [allowance_value])))
            return out

        monkeypatch.setattr("tools.definitions.get_token_approvals.multicall3_batch", _mock_batch)
        monkeypatch.setattr("tools.definitions.get_token_approvals.get_web3", MagicMock())

    async def test_zero_allowance_returns_empty(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, 0)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )

        assert result["approvals"] == []
        assert result["risk_summary"]["total_approvals"] == 0

    async def test_nonzero_bounded_allowance_returned(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _BOUNDED)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )

        assert len(result["approvals"]) == 1
        entry = result["approvals"][0]
        assert entry["allowance_raw"] == str(_BOUNDED)
        assert entry["is_unlimited"] is False
        assert entry["risk_level"] == "low"

    async def test_unlimited_approval_detected(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _UINT256_MAX)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )

        entry = result["approvals"][0]
        assert entry["is_unlimited"] is True
        assert entry["allowance_formatted"] == "unlimited"
        # Known spender (Uniswap Universal Router v1.2) → medium, not high.
        assert entry["risk_level"] == "medium"
        assert entry["spender_name"] == "Uniswap Universal Router v1.2"

    async def test_unlimited_unknown_spender_is_high_risk(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _UINT256_MAX)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNKNOWN],
        )

        entry = result["approvals"][0]
        assert entry["is_unlimited"] is True
        assert entry["risk_level"] == "high"
        assert entry["spender_name"] is None
        assert result["risk_summary"]["high_risk_approvals"] == 1
        assert result["risk_summary"]["unknown_spenders"] == 1

    async def test_permit2_spender_flagged(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _UINT256_MAX)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_PERMIT2],
        )

        entry = result["approvals"][0]
        assert entry["is_permit2"] is True
        assert entry["spender_name"] == "Permit2"
        # Permit2 is trusted → medium risk even with unlimited allowance.
        assert entry["risk_level"] == "medium"
        assert "Permit2" in result["note"]

    async def test_reverted_allowance_gracefully_skipped(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _BOUNDED, fail_index=0)

        # Should not raise — single reverted entry is skipped without global error.
        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC, "0xdAC17F958D2ee523a2206206994597C13D831ec7"],
            spenders=[_SPENDER_UNISWAP],
        )
        assert result["risk_summary"]["total_approvals"] == 1
        assert result["error"] is None

    async def test_full_batch_failure_surfaces_error(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, 0, fail=True)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )

        assert result["approvals"] == []
        assert result["risk_summary"]["total_approvals"] == 0
        assert "RPC batch failed" in (result["error"] or "")

    async def test_multicall3_batch_called_once(self, test_settings, monkeypatch):
        """multicall3_batch should be invoked exactly once per get_token_approvals call."""
        from tools.definitions.get_token_approvals import get_token_approvals

        call_count = 0

        async def _counting_batch(w3, calls, *, allow_failure=True, chain_id=None):
            nonlocal call_count
            call_count += 1
            return [(True, abi_encode(["uint256"], [_BOUNDED])) for _ in calls]

        monkeypatch.setattr("tools.definitions.get_token_approvals.multicall3_batch", _counting_batch)
        monkeypatch.setattr("tools.definitions.get_token_approvals.get_web3", MagicMock())

        await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )
        assert call_count == 1

    async def test_risk_summary_counts_correct(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        # Two tokens, two spenders: 4 pairs.
        # Mock returns _UINT256_MAX for all pairs.
        TOKEN_2 = "0xdAC17F958D2ee523a2206206994597C13D831ec7"  # USDT (checksummed)

        self._patch_batch(monkeypatch, _UINT256_MAX)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC, TOKEN_2],
            # One known (Uniswap = medium), one unknown (high).
            spenders=[_SPENDER_UNISWAP, _SPENDER_UNKNOWN],
        )

        summary = result["risk_summary"]
        assert summary["total_approvals"] == 4
        assert summary["unlimited_approvals"] == 4
        # 2 high-risk (both tokens × unknown spender).
        assert summary["high_risk_approvals"] == 2
        assert summary["unknown_spenders"] == 2

    async def test_high_risk_sorted_first(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _UINT256_MAX)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNKNOWN, _SPENDER_UNISWAP],
        )

        levels = [a["risk_level"] for a in result["approvals"]]
        # high should come before medium in sorted output.
        assert levels.index("high") < levels.index("medium")

    async def test_custom_token_list_respected(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        captured_calls: list = []

        async def _capture_batch(w3, calls, *, allow_failure=True, chain_id=None):
            captured_calls.extend(calls)
            return [(True, abi_encode(["uint256"], [0])) for _ in calls]

        monkeypatch.setattr("tools.definitions.get_token_approvals.multicall3_batch", _capture_batch)
        monkeypatch.setattr("tools.definitions.get_token_approvals.get_web3", MagicMock())

        CUSTOM_TOKEN = "0x6B175474E89094C44Da98b954EedeAC495271d0F"  # DAI

        await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[CUSTOM_TOKEN],
            spenders=[_SPENDER_UNISWAP],
        )

        # The Multicall3 call should have targeted the custom token address.
        used_targets = {call[0] for call in captured_calls}
        assert CUSTOM_TOKEN in used_targets

    async def test_empty_result_for_unsupported_chain(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        # Chain 999 has no tracked tokens and no trusted spenders.
        result = await get_token_approvals(
            wallet_address=_WALLET,
            chain_id=999,
        )

        assert result["approvals"] == []
        assert "No tokens or spenders" in result["note"]

    async def test_token_symbol_resolved_from_tracked_list(self, test_settings, monkeypatch):
        from tools.definitions.get_token_approvals import get_token_approvals

        self._patch_batch(monkeypatch, _UINT256_MAX)

        result = await get_token_approvals(
            wallet_address=_WALLET,
            tokens=[_TOKEN_USDC],
            spenders=[_SPENDER_UNISWAP],
        )

        entry = result["approvals"][0]
        # USDC is in the tracked list — symbol should be resolved.
        assert entry["token_symbol"] == "USDC"
