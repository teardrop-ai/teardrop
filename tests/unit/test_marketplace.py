"""Unit tests for marketplace.py — author config, earnings, withdrawals.

All external interactions (DB pool) are mocked so this suite runs without
a live Postgres instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace import (
    AuthorConfig,
    MarketplaceTool,
    get_author_balance,
    record_tool_call_earnings,
    request_withdrawal,
    set_author_config,
    validate_eip55_address,
)

_NOW = datetime.now(timezone.utc)

# Use a well-known address that passes EIP-55 when all-lowercase (all digits = no check).
_VALID_ADDR = "0x1234567890123456789012345678901234567890"

# ─── validate_eip55_address ───────────────────────────────────────────────────


class TestValidateEip55Address:
    def test_valid_numeric_address_passes(self):
        # All-numeric hex has no checksum letters to validate
        err = validate_eip55_address(_VALID_ADDR)
        assert err is None

    def test_zero_address_rejected(self):
        addr = "0x" + "00" * 20
        err = validate_eip55_address(addr)
        assert err is not None
        assert "zero" in err.lower()

    def test_too_short_rejected(self):
        err = validate_eip55_address("0x1234")
        assert err is not None

    def test_invalid_hex_rejected(self):
        addr = "0x" + "zz" * 20
        err = validate_eip55_address(addr)
        assert err is not None


# ─── set_author_config ────────────────────────────────────────────────────────


class TestSetAuthorConfig:
    @pytest.mark.anyio
    async def test_invalid_wallet_raises(self, monkeypatch):
        mock_pool = MagicMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)
        with pytest.raises(ValueError, match="wallet|address"):
            await set_author_config(
                "org-1",
                settlement_wallet="bad-address",
                revenue_share_bps=7000,
            )

    @pytest.mark.anyio
    async def test_bps_out_of_range_raises(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)
        with pytest.raises(ValueError, match="bps"):
            await set_author_config("org-1", settlement_wallet=_VALID_ADDR, revenue_share_bps=15000)

    @pytest.mark.anyio
    async def test_bps_negative_raises(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)
        with pytest.raises(ValueError, match="bps"):
            await set_author_config("org-1", settlement_wallet=_VALID_ADDR, revenue_share_bps=-1)

    @pytest.mark.anyio
    async def test_success_returns_config(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        config = await set_author_config(
            "org-1",
            settlement_wallet=_VALID_ADDR,
            revenue_share_bps=7000,
        )
        assert isinstance(config, AuthorConfig)
        assert config.settlement_wallet == _VALID_ADDR
        assert config.revenue_share_bps == 7000


# ─── record_tool_call_earnings ────────────────────────────────────────────────


class TestRecordToolCallEarnings:
    @pytest.mark.anyio
    async def test_no_author_config_skips(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        # Should not raise
        await record_tool_call_earnings("author-org", "caller-org", "my_tool", 1000)
        # No INSERT should have been called
        mock_pool.execute.assert_not_called()

    @pytest.mark.anyio
    async def test_records_correct_split(self, monkeypatch):
        config_row = {
            "org_id": "author-org",
            "settlement_wallet": _VALID_ADDR,
            "revenue_share_bps": 7000,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=config_row)
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await record_tool_call_earnings("author-org", "caller-org", "my_tool", 10_000)
        mock_pool.execute.assert_called_once()

        # Check the SQL args for correct split
        call_args = mock_pool.execute.call_args
        # Positional args:
        # (SQL, uuid, org_id, tool_name, caller_org_id,
        #  total_cost, author_share, platform_share)
        args = call_args[0]
        # author_share = 10000 * 7000 // 10000 = 7000
        # platform_share = 10000 - 7000 = 3000
        assert args[6] == 7000  # author_share_usdc
        assert args[7] == 3000  # platform_share_usdc


# ─── get_author_balance ───────────────────────────────────────────────────────


class TestGetAuthorBalance:
    @pytest.mark.anyio
    async def test_zero_balance(self, monkeypatch):
        mock_pool = MagicMock()
        # COALESCE in SQL returns 0 when no rows match
        mock_pool.fetchval = AsyncMock(return_value=0)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        balance = await get_author_balance("org-1")
        assert balance == 0

    @pytest.mark.anyio
    async def test_returns_sum(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.fetchval = AsyncMock(return_value=50_000)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        balance = await get_author_balance("org-1")
        assert balance == 50_000


# ─── request_withdrawal ──────────────────────────────────────────────────────


class TestRequestWithdrawal:
    @pytest.mark.anyio
    async def test_no_author_config_raises(self, monkeypatch):
        # get_author_config returns None
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        with pytest.raises(ValueError, match="Author config not set"):
            await request_withdrawal("org-1", 200_000)


# ─── MarketplaceTool model ───────────────────────────────────────────────────


class TestMarketplaceToolModel:
    def test_qualified_name(self):
        t = MarketplaceTool(
            name="my_tool",
            qualified_name="acme/my_tool",
            description="desc",
            marketplace_description="mp desc",
            input_schema={"type": "object"},
            cost_usdc=100,
            author_org_name="Acme Corp",
            author_org_slug="acme",
        )
        assert t.qualified_name == "acme/my_tool"
        assert t.cost_usdc == 100
