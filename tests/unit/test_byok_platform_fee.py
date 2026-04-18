"""Unit tests for BYOK platform fee — get_byok_platform_fee() and billing flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from billing import get_byok_platform_fee

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
        """Mirror the debit_amount logic from app.py."""
        return platform_fee if is_byok else cost_usdc

    def test_byok_debits_platform_fee_only(self):
        assert self._debit_amount(is_byok=True, platform_fee=1000, cost_usdc=50_000) == 1000

    def test_non_byok_debits_full_cost(self):
        assert self._debit_amount(is_byok=False, platform_fee=0, cost_usdc=50_000) == 50_000

    def test_non_byok_platform_fee_always_zero(self):
        """get_byok_platform_fee returns 0 for non-BYOK, so platform_fee==0."""
        fee = get_byok_platform_fee(is_byok=False)
        assert self._debit_amount(is_byok=False, platform_fee=fee, cost_usdc=25_000) == 25_000
