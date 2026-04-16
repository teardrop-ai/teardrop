"""Unit tests for marketplace.py — author config, earnings, withdrawals.

All external interactions (DB pool) are mocked so this suite runs without
a live Postgres instance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from marketplace import (
    AuthorConfig,
    AuthorWithdrawal,
    MarketplaceTool,
    complete_withdrawal,
    get_author_balance,
    list_pending_withdrawals,
    process_withdrawal,
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

    def test_valid_checksummed_address_passes(self):
        # EIP-55 canonical test vector — exercises the Keccak-256 checksum path
        err = validate_eip55_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")
        assert err is None

    def test_bad_checksum_rejected(self):
        # All-lowercase version of a checksummed address must fail
        err = validate_eip55_address("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")
        assert err is not None
        assert "checksum" in err.lower()

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
        await record_tool_call_earnings(
            author_org_id="author-org",
            tool_name="my_tool",
            caller_org_id="caller-org",
            total_cost_usdc=1000,
        )
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

        await record_tool_call_earnings(
            author_org_id="author-org",
            tool_name="my_tool",
            caller_org_id="caller-org",
            total_cost_usdc=10_000,
        )
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

    @pytest.mark.anyio
    async def test_below_minimum_raises(self, monkeypatch):
        config_row = {
            "org_id": "org-1",
            "settlement_wallet": _VALID_ADDR,
            "revenue_share_bps": 7000,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=config_row)
        monkeypatch.setattr("marketplace._pool", mock_pool)
        # Default minimum is 100_000; request 50_000 (below minimum)
        with pytest.raises(ValueError, match="[Mm]inimum"):
            await request_withdrawal("org-1", 50_000)

    @pytest.mark.anyio
    async def test_insufficient_balance_raises(self, monkeypatch):
        config_row = {
            "org_id": "org-1",
            "settlement_wallet": _VALID_ADDR,
            "revenue_share_bps": 7000,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        mock_pool = MagicMock()
        # First fetchrow → config; fetchval → low balance
        mock_pool.fetchrow = AsyncMock(return_value=config_row)
        mock_pool.fetchval = AsyncMock(return_value=50_000)  # only $0.05 pending
        monkeypatch.setattr("marketplace._pool", mock_pool)

        with pytest.raises(ValueError, match="[Ii]nsufficient"):
            await request_withdrawal("org-1", 200_000)  # requests $0.20

    @pytest.mark.anyio
    async def test_cooldown_raises(self, monkeypatch):
        config_row = {
            "org_id": "org-1",
            "settlement_wallet": _VALID_ADDR,
            "revenue_share_bps": 7000,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        recent_withdrawal = {
            "created_at": datetime.now(timezone.utc) - timedelta(seconds=30)
        }
        mock_pool = MagicMock()
        # First fetchrow → config; fetchval → sufficient balance;
        # second fetchrow → recent withdrawal (triggers cooldown)
        mock_pool.fetchrow = AsyncMock(side_effect=[config_row, recent_withdrawal])
        mock_pool.fetchval = AsyncMock(return_value=500_000)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        with pytest.raises(ValueError, match="[Cc]ooldown"):
            await request_withdrawal("org-1", 200_000)


# ─── process_withdrawal ─────────────────────────────────────────────────────


def _make_conn_mock(withdrawal_row, earnings_rows):
    """Build pool+conn mocks for process_withdrawal, which uses acquire()/transaction()."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=earnings_rows)
    mock_conn.execute = AsyncMock()

    # conn.transaction() must be an async context manager
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=False)
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    # pool.acquire() must be an async context manager yielding conn
    mock_acquire = AsyncMock()
    mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_acquire.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.fetchrow = AsyncMock(return_value=withdrawal_row)
    mock_pool.acquire = MagicMock(return_value=mock_acquire)
    return mock_pool, mock_conn


class TestProcessWithdrawal:
    @pytest.mark.anyio
    async def test_not_found_raises(self, monkeypatch):
        mock_pool, _ = _make_conn_mock(withdrawal_row=None, earnings_rows=[])
        monkeypatch.setattr("marketplace._pool", mock_pool)

        with pytest.raises(ValueError, match="not found"):
            await process_withdrawal("missing-id")

    @pytest.mark.anyio
    async def test_best_effort_settlement_skips_oversized_rows(self, monkeypatch):
        """Rows [$500, $600, $200] with withdrawal $700 → settles e1+e3, skips e2.

        Verifies the P1 best-effort fix: loop continues past oversized rows
        instead of breaking, so smaller later rows are still settled.
        """
        withdrawal_row = {
            "id": "w-1",
            "org_id": "org-1",
            "amount_usdc": 700,
            "wallet": _VALID_ADDR,
            "status": "pending",
            "created_at": _NOW,
            "settled_at": None,
        }
        earnings_rows = [
            {"id": "e1", "author_share_usdc": 500},  # fits (remaining: 700 → 200)
            {"id": "e2", "author_share_usdc": 600},  # skipped (600 > 200)
            {"id": "e3", "author_share_usdc": 200},  # fits (remaining: 200 → 0)
        ]
        mock_pool, mock_conn = _make_conn_mock(withdrawal_row, earnings_rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await process_withdrawal("w-1")

        # Two execute calls: UPDATE earnings + UPDATE withdrawal
        assert mock_conn.execute.call_count == 2
        first_call_args = mock_conn.execute.call_args_list[0].args
        settled_ids = first_call_args[1]  # second arg to first execute
        assert sorted(settled_ids) == ["e1", "e3"]

    @pytest.mark.anyio
    async def test_marks_withdrawal_settled(self, monkeypatch):
        withdrawal_row = {
            "id": "w-2",
            "org_id": "org-1",
            "amount_usdc": 300,
            "wallet": _VALID_ADDR,
            "status": "pending",
            "created_at": _NOW,
            "settled_at": None,
        }
        earnings_rows = [{"id": "e1", "author_share_usdc": 300}]
        mock_pool, mock_conn = _make_conn_mock(withdrawal_row, earnings_rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        result = await process_withdrawal("w-2")

        assert result.status == "settled"
        assert result.settled_at is not None
        # Withdrawal UPDATE is the second execute call
        second_call_sql = mock_conn.execute.call_args_list[1].args[0]
        assert "tool_author_withdrawals" in second_call_sql


# ─── complete_withdrawal ──────────────────────────────────────────────────────


class TestCompleteWithdrawal:
    @pytest.mark.anyio
    async def test_records_tx_hash(self, monkeypatch):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock()
        monkeypatch.setattr("marketplace._pool", mock_pool)

        await complete_withdrawal("w-1", "0xdeadbeefdeadbeef")

        mock_pool.execute.assert_called_once()
        call_args = mock_pool.execute.call_args.args
        assert call_args[1] == "w-1"       # withdrawal_id
        assert call_args[2] == "0xdeadbeefdeadbeef"  # tx_hash


# ─── list_pending_withdrawals ─────────────────────────────────────────────────


class TestListPendingWithdrawals:
    def _make_withdrawal_row(self, org_id: str = "org-1") -> dict:
        return {
            "id": "w-1",
            "org_id": org_id,
            "amount_usdc": 200_000,
            "tx_hash": "",
            "wallet": _VALID_ADDR,
            "status": "pending",
            "created_at": _NOW,
            "settled_at": None,
        }

    @pytest.mark.anyio
    async def test_no_filter_returns_all(self, monkeypatch):
        rows = [self._make_withdrawal_row("org-1"), self._make_withdrawal_row("org-2")]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        result = await list_pending_withdrawals()

        assert len(result) == 2
        # No org_id arg passed to fetch
        call_args = mock_pool.fetch.call_args
        assert call_args.args[0:1] == (call_args.args[0],)  # only SQL, no $1 param
        assert len(call_args.args) == 1  # SQL only, no positional binding args

    @pytest.mark.anyio
    async def test_org_filter(self, monkeypatch):
        rows = [self._make_withdrawal_row("org-1")]
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=rows)
        monkeypatch.setattr("marketplace._pool", mock_pool)

        result = await list_pending_withdrawals(org_id="org-1")

        assert len(result) == 1
        assert result[0].org_id == "org-1"
        # org_id should be passed as binding param
        call_args = mock_pool.fetch.call_args
        assert "org-1" in call_args.args


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
