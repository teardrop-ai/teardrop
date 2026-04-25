"""Unit tests for BYOK platform fee — get_byok_platform_fee() and billing flow."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from billing import calculate_byok_orchestration_cost, get_byok_platform_fee

# ─── get_byok_platform_fee ───────────────────────────────────────────────────


class TestGetByokPlatformFee:
    """Test the flat per-run fee lookup for BYOK orgs."""

    def test_non_byok_returns_zero(self):
        assert get_byok_platform_fee(is_byok=False) == 0

    def test_byok_returns_configured_fee(self):
        mock_settings = MagicMock()
        mock_settings.byok_platform_fee_usdc = 1000
        with patch("billing.get_settings", return_value=mock_settings):
            assert get_byok_platform_fee(is_byok=True) == 1000

    def test_byok_custom_fee(self):
        mock_settings = MagicMock()
        mock_settings.byok_platform_fee_usdc = 5000
        with patch("billing.get_settings", return_value=mock_settings):
            assert get_byok_platform_fee(is_byok=True) == 5000

    def test_byok_zero_fee(self):
        mock_settings = MagicMock()
        mock_settings.byok_platform_fee_usdc = 0
        with patch("billing.get_settings", return_value=mock_settings):
            assert get_byok_platform_fee(is_byok=True) == 0

    def test_non_byok_never_reads_settings(self):
        """Ensure no settings lookup when is_byok=False (fast path)."""
        with patch("billing.get_settings") as mock_get:
            get_byok_platform_fee(is_byok=False)
            mock_get.assert_not_called()


# ─── Debit amount logic ──────────────────────────────────────────────────────


class TestByokDebitAmount:
    """Verify the debit_amount formula used in app.py settlement."""

    @staticmethod
    def _debit_amount(is_byok: bool, platform_fee: int, cost_usdc: int) -> int:
        """Mirror the legacy debit_amount logic from app.py."""
        return platform_fee if is_byok else cost_usdc

    def test_byok_debits_platform_fee_only(self):
        assert self._debit_amount(is_byok=True, platform_fee=1000, cost_usdc=50_000) == 1000

    def test_non_byok_debits_full_cost(self):
        assert self._debit_amount(is_byok=False, platform_fee=0, cost_usdc=50_000) == 50_000

    def test_non_byok_platform_fee_always_zero(self):
        """get_byok_platform_fee returns 0 for non-BYOK, so platform_fee==0."""
        fee = get_byok_platform_fee(is_byok=False)
        assert self._debit_amount(is_byok=False, platform_fee=fee, cost_usdc=25_000) == 25_000


# ─── calculate_byok_orchestration_cost ──────────────────────────────────────


def _run(coro):
    """Helper: run a coroutine synchronously in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestCalculateByokOrchestrationCost:
    """Token-based BYOK orchestration cost (migration 041)."""

    def _make_settings(self, floor: int = 1000) -> MagicMock:
        s = MagicMock()
        s.byok_platform_fee_usdc = floor
        s.pricing_cache_ttl_seconds = 60
        return s

    def _make_rule(self, tokens_in_cost=50, tokens_out_cost=50) -> MagicMock:
        rule = MagicMock()
        rule.tokens_in_cost_per_1k = tokens_in_cost
        rule.tokens_out_cost_per_1k = tokens_out_cost
        return rule

    def test_falls_back_to_floor_when_no_rule(self):
        """When no BYOK pricing rule exists, return the configured floor."""
        settings = self._make_settings(floor=1000)
        with patch("billing.get_settings", return_value=settings):
            with patch("billing.get_live_pricing_for_model", new=AsyncMock(return_value=None)):
                result = _run(calculate_byok_orchestration_cost(0, 0, "anthropic", "claude-3-5-haiku"))
        assert result == 1000

    def test_token_cost_below_floor_returns_floor(self):
        """When computed token cost < floor, floor wins."""
        settings = self._make_settings(floor=1000)
        # 500 tokens_in, 200 tokens_out: computed = 0 + 0 = 0 (less than 1k)
        rule = self._make_rule(tokens_in_cost=50, tokens_out_cost=50)
        with patch("billing.get_settings", return_value=settings):
            with patch("billing.get_live_pricing_for_model", new=AsyncMock(return_value=rule)):
                result = _run(calculate_byok_orchestration_cost(500, 200, "openai", "gpt-4o"))
        # 500 // 1000 = 0 units; 200 // 1000 = 0 units → computed=0 → floor=1000
        assert result == 1000

    def test_token_cost_above_floor(self):
        """When computed token cost > floor, token cost is returned."""
        settings = self._make_settings(floor=100)
        # 2000 tokens_in, 3000 tokens_out at 50/1k each
        # computed = (2000//1000)*50 + (3000//1000)*50 = 100 + 150 = 250
        # floor=100 < computed=250, so token cost wins
        rule = self._make_rule(tokens_in_cost=50, tokens_out_cost=50)
        with patch("billing.get_settings", return_value=settings):
            with patch("billing.get_live_pricing_for_model", new=AsyncMock(return_value=rule)):
                result = _run(calculate_byok_orchestration_cost(2000, 3000, "anthropic", ""))
        assert result == 250

    def test_large_run_scales_correctly(self):
        """Verify linear scaling with high token counts."""
        settings = self._make_settings(floor=0)  # no floor to test pure token math
        # 100k tokens_in + 50k tokens_out at 50/1k each
        # computed = 100*50 + 50*50 = 5000 + 2500 = 7500
        rule = self._make_rule(tokens_in_cost=50, tokens_out_cost=50)
        with patch("billing.get_settings", return_value=settings):
            with patch("billing.get_live_pricing_for_model", new=AsyncMock(return_value=rule)):
                result = _run(calculate_byok_orchestration_cost(100_000, 50_000, "openai", ""))
        assert result == 7500

    def test_non_byok_unaffected(self):
        """calculate_byok_orchestration_cost is only called for BYOK orgs; ensure
        get_byok_platform_fee still returns 0 for non-BYOK orgs."""
        assert get_byok_platform_fee(is_byok=False) == 0

    def test_zero_floor_returns_zero_for_tiny_runs(self):
        """With floor=0 and < 1k tokens, result should be exactly 0."""
        settings = self._make_settings(floor=0)
        rule = self._make_rule(tokens_in_cost=50, tokens_out_cost=50)
        with patch("billing.get_settings", return_value=settings):
            with patch("billing.get_live_pricing_for_model", new=AsyncMock(return_value=rule)):
                result = _run(calculate_byok_orchestration_cost(100, 100, "google", ""))
        assert result == 0

